import os
import yaml
import json
import logging
import shortuuid
import copy
import torch
import argparse
import shutil

from dataclasses import dataclass
from typing import List, Dict
from socket import gethostname

try:
    from typing import Literal
except ImportError:
    from typing_extensions import Literal
from deepspeed.launcher.runner import DLTS_HOSTFILE
from neox.logging import Tee
from neox.tokenizer import build_tokenizer
from neox.utils import obtain_resource_pool, expand_attention_types, Timers
from .deepspeed_args import NeoXArgsDeepspeedConfig, NeoXArgsDeepspeedRunner
from .neox_args import (
    NeoXArgsModel,
    NeoXArgsTokenizer,
    NeoXArgsTraining,
    NeoXArgsParallelism,
    NeoXArgsLogging,
    NeoXArgsOther,
    NeoXArgsTextgen,
    NeoXArgsOptimizer,
    NeoXArgsLRScheduler,
    ATTENTION_TYPE_CHOICES,
)

# ZERO defaults by deespeed
# These values should not be changed unless defaults in deepspeed are changed
ZERO_DEFAULTS = {
    "stage": 0,
    "allgather_partitions": True,
    "reduce_scatter": True,
    "allgather_bucket_size": int(5e8),
    "overlap_comm": False,
    "reduce_scatter": True,
    "reduce_bucket_size": int(5e8),
    "contiguous_gradients": False,
    "cpu_offload": False,
}

# NeoX optimizer defaults
OPT_DEFAULT = "Adam"
OPT_PARAMS_DEFAULTS = {
    "lr": 0.001,
    "betas": [0.9, 0.999],
    "eps": 1.0e-8,
    "weight_decay": 0,
    "freeze_step": 400,
    "momentum": 0.0,
    "cuda_aware": False,
}

BASE_CLASSES = [
    NeoXArgsDeepspeedRunner,
    NeoXArgsDeepspeedConfig,
    NeoXArgsModel,
    NeoXArgsLRScheduler,
    NeoXArgsOptimizer,
    NeoXArgsTokenizer,
    NeoXArgsTraining,
    NeoXArgsParallelism,
    NeoXArgsLogging,
    NeoXArgsTextgen,
    NeoXArgsOther,
]

DEEPSPEED_ARG_CLASSES = [NeoXArgsDeepspeedRunner, NeoXArgsDeepspeedConfig]
NEOX_ARG_CLASSES = [i for i in BASE_CLASSES if i not in DEEPSPEED_ARG_CLASSES]


@dataclass
class NeoXArgs(*BASE_CLASSES):
    """
    data class containing all configurations

    NeoXArgs inherits from a number of small configuration classes
    """

    ############################################################################################################################
    # start of instantiation

    def __post_init__(self):
        """
        after initialization of default or loaded values
        a number of functions are performed in order to
        calculate values, assert consistency and do typechecking.
        """
        if not NeoXArgs.validate_keys():
            raise ValueError(
                self.__class__.__name__
                + ".__post_init__() NeoXArgs keys cannot be validated"
            )

        self.enable_logging()

        self.calculate_derived()

        if not self.validate_types():
            raise ValueError(
                self.__class__.__name__
                + ".__post_init__() NeoXArgs types cannot be validated"
            )

        if not self.validate_values():
            raise ValueError(
                self.__class__.__name__
                + ".__post_init__() NeoXArgs values cannot be validated"
            )

        # initialize non-configurable values
        self.timers = None

    def build_tokenizer(self):
        self.tokenizer = build_tokenizer(self)

    def initialize_timers(self):
        self.timers = Timers(
            use_wandb=self.use_wandb,
            tensorboard_writer=self.tensorboard_writer,
        )

    def initialize_tensorboard_writer(self):
        if self.tensorboard_dir and self.rank == 0:
            try:
                from torch.utils.tensorboard import SummaryWriter

                print("> setting tensorboard ...")
                self.tensorboard_writer = SummaryWriter(log_dir=self.tensorboard_dir)
            except (ModuleNotFoundError, ImportError):
                print(
                    "WARNING: TensorBoard writing requested but is not "
                    "available (are you using PyTorch 1.1.0 or later and do you have tensorboard installed?), "
                    "no TensorBoard logs will be written.",
                    flush=True,
                )

    def initialize_wandb(self):
        """
        Initialize wandb if configured.
        """
        # Wandb. (one worker per machine)
        if self.use_wandb == False:
            return

        import wandb
        import socket
        from ..utils import is_local_main, get_wandb_api_key

        # only initialize wandb if we are the main process for the local rank and a valid api key is provided
        use_wandb = is_local_main() and (get_wandb_api_key(neox_args=self) is not None)
        self.update_value("use_wandb", use_wandb)
        if self.use_wandb:
            group_name = self.wandb_group
            # get a unique name for each rank
            name = f"{socket.gethostname()}-{self.local_rank}" if group_name else None

            # initialize wandb
            try:
                config = self.all_config
                config["num_params"] = getattr(
                    self, "total_params", None
                )  # get number of parameters in the model, if it's been calculated
                wandb.init(
                    project=self.wandb_project,
                    group=group_name,
                    name=name,
                    save_code=False,
                    force=False,
                    entity=self.wandb_team,
                    config=config,
                )
            except wandb.UsageError as e:
                self.update_value("use_wandb", False)
                print(e)
                print(
                    "Skipping wandb. Execute `wandb login` on local or main node machine to enable.",
                    flush=True,
                )

    @classmethod
    def from_ymls(cls, paths_to_yml_files: List[str], overwrite_values: Dict = None):
        """
        instantiates NeoXArgs while reading values from yml files

        paths_to_yml_files: list of paths to yml files

        overwrite_values: If provided, overwrite any values in the yamls with these values
        """

        print(cls.__name__ + ".from_ymls() " + str(paths_to_yml_files), flush=True)

        # initialize an empty config dictionary to be filled by yamls
        config = dict()
        config_files = dict()
        # iterate of all to be loaded yaml files
        for conf_file_name in paths_to_yml_files:

            # load file
            with open(conf_file_name) as conf_file:
                conf = yaml.load(conf_file, Loader=yaml.FullLoader)

            # check for key duplicates and load values
            for conf_key, conf_value in conf.items():
                if conf_key in config:
                    raise ValueError(
                        f"Conf file {conf_file_name} has the following duplicate keys with previously loaded file: {conf_key}"
                    )

                conf_key_converted = conf_key.replace(
                    "-", "_"
                )  # TODO remove replace and update configuration files?
                config[conf_key_converted] = conf_value

            # load original config files to save unchanged with checkpoint
            # saving the original config retains comments
            filename = os.path.basename(conf_file_name)
            assert filename not in config_files, "At least two config files have the same filename. This will result in conflicts when saving out configs with the checkpoint in one single directory. Please use unique names for configs."
            config_files[filename] = open(conf_file_name).read()

        # add config file content to neox args to make them accessible in code
        # this is used when saving checkpoints
        config["config_files"] = config_files
        
        # Configuration parameters not specified
        params_not_in_config = sorted(
            list(set(cls.__dataclass_fields__.keys()) - set(config.keys()))
        )
        if len(params_not_in_config) > 0:
            logging.debug(
                cls.__name__
                + ".from_ymls() Configuration parameters not specified (using defaults): "
                + ", ".join(params_not_in_config)
            )

        if overwrite_values is not None:
            for k, v in overwrite_values.items():
                config[k] = v

        # instantiate class and return
        # duplicate values and unrecognized keys are again checked upon instantiation
        return cls(**config)

    @classmethod
    def from_dict(cls, args_dict: Dict):
        """
        instantiates NeoXArgs while reading values from input dict
        """
        return cls(**args_dict)

    ############################################################################################################################
    # start of command line args interface

    @classmethod
    def parse_args(cls):
        """
        Parses command line arguments from ./deepy.py or the deepspeed launcher (user script, configs, etc.) and returns a NeoXArgs object.

        GPT-NeoX Configuration

        optional arguments:
          -h, --help            show this help message and exit

        Training Configuration:
          user_script           User script to launch, followed by any required arguments.
          --conf_dir CONF_DIR, -d CONF_DIR
                                Directory to prefix to all configuration file paths
          conf_file             Configuration file path. Multiple files can be provided and will be merged.

        Weights and Biases monitoring args:
          --wandb_group WANDB_GROUP
                                Weights and Biases group name - used to group together runs.
          --wandb_team WANDB_TEAM
                                Team name for Weights and Biases.
          --eval_tasks EVAL_TASKS [EVAL_TASKS ...]
                                Optionally overwrite eval tasks to run for evaluate.py
        """

        parser = argparse.ArgumentParser(
            description="GPT-NeoX Configuration", allow_abbrev=False
        )

        group = parser.add_argument_group(title="Training Configuration")

        group.add_argument(
            "user_script",
            type=str,
            help="User script to launch, followed by any required " "arguments.",
        )

        group.add_argument(
            "--conf_dir",
            "-d",
            type=str,
            default=None,
            help="Directory to prefix to all configuration file paths",
        )

        group.add_argument(
            "conf_file",
            type=str,
            nargs="+",
            help="Configuration file path. Multiple files can be provided and will be merged.",
        )

        group = parser.add_argument_group(title="Weights and Biases monitoring args")

        group.add_argument(
            "--wandb_group",
            type=str,
            default=None,
            help="Weights and Biases group name - used to group together runs.",
        )
        group.add_argument(
            "--wandb_team",
            type=str,
            default=None,
            help="Team name for Weights and Biases.",
        )

        group.add_argument(
            "--eval_tasks",
            type=str,
            nargs="+",
            default=None,
            help="Optionally overwrite eval tasks to run for evaluate.py",
        )
        group.add_argument(
            "--iteration",
            type=int,
            default=None,
            help="Iteration to load checkpoint from in evaluate.py / generate.py. If None is provided, uses the latest iteration.",
        )
        group.add_argument(
            "--eval_results_prefix",
            type=str,
            default=None,
            help="prefix to append to eval results file",
        )
        args_parsed = parser.parse_args()

        # Validate user_script exists
        assert os.path.exists(
            args_parsed.user_script
        ), f"User script could not be found: {args_parsed.user_script}"

        # load config files
        conf_files = args_parsed.conf_file
        if args_parsed.conf_dir:
            conf_files = [os.path.join(args_parsed.conf_dir, f) for f in conf_files]

        # enables us to pass in `small` instead of `small.yml`
        conf_files = [(cf if cf.endswith(".yml") else cf + ".yml") for cf in conf_files]

        # determine overwrite values
        overwrite_values = dict()
        for k, v in vars(args_parsed).items():
            if k not in ["conf_dir", "conf_file"] and v is not None:
                overwrite_values[k] = v

        # load args
        neox_args = cls.from_ymls(
            paths_to_yml_files=conf_files, overwrite_values=overwrite_values
        )

        # save a copy of yaml configs to the save directory
        if neox_args.save is not None:
            configs_directory = os.path.join(neox_args.save, "configs")

            # If loading the conf files from the save directory
            # deleting the conf files in the following step would
            # naturally prevent the later copy. Therefore we are first
            # loading the files into memory.
            conf_files_memory = dict()
            for conf_file in conf_files:
                conf_files_memory[os.path.basename(conf_file)] = open(
                    conf_file, "r"
                ).read()

            # Delete the configs subdirectory in save if it already exists.
            # Reason: only the latest version of the configs are stored
            # All files are deleted because selecting a subset of configs
            # is a valid option. We would like to prevent keeping files
            # which are not part of the latest config. If data is saved to
            # a previously non-empty save directory.
            if os.path.isdir(configs_directory):
                shutil.rmtree(configs_directory)

            # create configs directory and save config files
            os.makedirs(configs_directory)
            for conf_file_name, conf_data in conf_files_memory.items():
                with open(os.path.join(configs_directory, conf_file_name), "w") as f:
                    f.write(conf_data)

        if neox_args.wandb_group is not None:
            # concat the wandb group name with a uid to make sure it's unique
            import wandb

            neox_args.wandb_group += "_" + wandb.util.generate_id()
        neox_args.print()

        return neox_args

    @classmethod
    def from_launcher_args(
        cls,
        overwrite_values: dict = None,
        configure_distributed_args: bool = True,
        build_tokenizer: bool = True,
        initialize_tensorboard_writer: bool = False,
        initialize_wandb: bool = False,
        initialize_timers: bool = False,
    ):
        """
        Parses the .json neox config sent by the deepspeed launcher to all workers and returns a NeoXArgs object.

        The .yaml configuration is first read from the main rank, then serialized into a dictionary, which the deepspeed launcher then broadcasts
        to all machines (`--neox_config`).

        We then instantiate a new NeoXArgs from the dictionary (`.from_dict`). This should ensure args are never inconsistent across machines,
        as they may be if a config file was loaded from the disks of each worker.

        Args:
            cls: self
            overwrite_values: dict of values to overwrite in the config.
            configure_distributed_args: whether to parse distributed args from environment variables (e.g. `world_size` and `rank`).
            build_tokenizer: whether to build the tokenizer specified in the config.
            initialize_tensorboard_writer: whether to initialize a tensorboard writer (if specified in the config).
            initialize_wandb: whether to initialize wandb (if specified in the config).

        Returns:
            neox_args: a new NeoXArgs instance
        """

        parser = argparse.ArgumentParser(
            description="GPT-NeoX Configuration", allow_abbrev=False
        )
        parser.add_argument(
            "--neox_config",
            type=str,
            default=None,
            help="json dict dumped as string in NeoXArgs.get_deepspeed_main_args()",
        )

        args_parsed, _ = parser.parse_known_args()
        neox_config = json.loads(args_parsed.neox_config)
        if overwrite_values is not None:
            neox_config.update(overwrite_values)
        neox_args = cls.from_dict(args_dict=neox_config)
        if configure_distributed_args:
            neox_args.configure_distributed_args()
        if build_tokenizer:
            neox_args.build_tokenizer()
        if initialize_tensorboard_writer:
            neox_args.initialize_tensorboard_writer()
        if initialize_wandb:
            neox_args.initialize_wandb()
        if initialize_timers:
            neox_args.initialize_timers()
        return neox_args

    @staticmethod
    def convert_key_value_to_command_line_arg(k, v):
        if isinstance(v, bool):
            if v:
                return [f"--{k}"]
            else:
                return []
        if v is None:
            return []
        return [f"--{k}", str(v)]

    def get_deepspeed_main_args(self):

        args_list = list()

        # get deepspeed runner args, and only pass them in to deepspeed launcher if they differ from defaults
        for key, default_value in NeoXArgsDeepspeedRunner().defaults():
            configured_value = getattr(self, key)
            if configured_value != default_value:
                args_list.extend(
                    self.convert_key_value_to_command_line_arg(key, configured_value)
                )

        if (
            "--include" in args_list or "--exclude" in args_list
        ) and "--num_gpus" in args_list:
            print(
                "WARNING: both --include/--exclude and num_gpus were specified simultaneously - overriding num_gpus with --include/--exclude"
            )
            # cannot specify these both simultaneously, remove num_gpus from list
            idx = args_list.index("--num_gpus")
            # pop twice, once for the arg, once for its value
            args_list.pop(idx)
            args_list.pop(idx)

        # add user script
        args_list.append(self.user_script)

        # get deepspeed_config
        args_list.append("--deepspeed_config")
        args_list.append(json.dumps(self.deepspeed_config))

        # get all config values
        args_list.append("--neox_config")
        neox_args = self.get_parent_class_value_dict(
            *self.__class__.__bases__, only_non_defaults=True
        )
        args_list.append(json.dumps(neox_args))

        return args_list

    ############################################################################################################################
    # start of calculated properties

    @property
    def deepspeed_config(self) -> dict:
        """
        returns a dict containing variables within deepspeed config
        """
        return self.get_parent_class_value_dict(
            NeoXArgsDeepspeedConfig, only_non_defaults=True
        )

    @property
    def deepspeed_runner(self) -> dict:
        """
        returns variables within deepspeed runner
        """
        return self.get_parent_class_value_dict(NeoXArgsDeepspeedRunner)

    @property
    def neox_config(self) -> dict:
        """
        returns variables within neox args
        """
        return self.get_parent_class_value_dict(*NEOX_ARG_CLASSES)

    @property
    def all_config(self) -> dict:
        """
        returns variables of all args
        """
        return self.get_parent_class_value_dict(*BASE_CLASSES)

    def get_parent_class_value_dict(
        self, *parent_classes, only_non_defaults=False
    ) -> dict:
        """
        takes a sequence of parent classes and returns corresponding values (with defaults set)
        """
        # TODO no Nones or non-defaults
        result = dict()
        for parent in parent_classes:
            for key, default_value in parent().defaults():
                if key in ["tokenizer", "tensorboard_writer", "adlr_autoresume_object"]:
                    continue
                if only_non_defaults:
                    value = getattr(self, key)
                    if value == default_value:
                        continue
                result[key] = getattr(self, key)
        return result

    @property
    def params_dtype(self):
        """
        returns the datatype on the basis of configured precision
        """
        if self.precision == "fp16":
            return torch.half
        elif self.precision == "bfloat16":
            return torch.bfloat16
        else:
            return torch.float

    ############################################################################################################################
    # start of logging and output

    def enable_logging(self):
        """
        enable Tee logs based on the configured logdir
        """
        if self.log_dir:
            os.makedirs(self.log_dir, exist_ok=True)
            hostname = gethostname()
            file_prefix = os.path.join(self.log_dir, hostname)
            Tee(file_prefix + "_stdout.txt", err=False)
            Tee(file_prefix + "_stderr.txt", err=True)

    def print(self):
        """Print arguments."""
        if self.rank == 0 or self.rank is None:
            print("-------------------- arguments --------------------", flush=True)
            str_list = []
            for arg in vars(self):
                # add arg + value
                dots = "." * (32 - len(arg))
                value = getattr(self, arg)
                print_str = "  {} {} {}".format(arg, dots, value)

                # add info 'default or updated'
                field_def = self.__dataclass_fields__.get(arg)
                if field_def is not None:
                    default_info = (
                        "default" if value == field_def.default else "updated"
                    )
                else:
                    default_info = ""
                dots = "." * (64 - len(print_str))
                print_str += dots
                str_list.append({"print_str": print_str, "default_info": default_info})

            for arg in sorted(
                sorted(str_list, key=lambda x: x["print_str"].lower()),
                key=lambda x: x["default_info"],
                reverse=True,
            ):
                print(arg["print_str"] + arg["default_info"], flush=True)
            print("---------------- end of arguments ----------------", flush=True)

    ############################################################################################################################
    # start of calculations and derived values

    def configure_distributed_args(self):
        """
        Parses the distributed training arguments (local rank, rank, world size, etc.) set as environment variables by the deepspeed launcher.
        """
        if self.deepspeed_mpi:
            from deepspeed.utils.distributed import mpi_discovery

            mpi_discovery()

        self.update_value("local_rank", int(os.getenv("LOCAL_RANK", "0")))
        self.update_value("rank", int(os.getenv("RANK", "0")))
        self.update_value("world_size", int(os.getenv("WORLD_SIZE", "1")))

        if self.rank == 0:
            print(
                self.__class__.__name__
                + ".configure_distributed_args() using world size: {} and model-parallel size: {} ".format(
                    self.world_size, self.model_parallel_size
                ),
                flush=True,
            )

    @staticmethod
    def calculate_batch_parameters(
        dp_world_size, train_batch=None, micro_batch=None, grad_acc=None
    ):
        # all values are provided nothing needs to be set
        if train_batch is not None and micro_batch is not None and grad_acc is not None:
            return train_batch, micro_batch, grad_acc

        # gradient_accumulation_steps needs to be set
        elif train_batch is not None and micro_batch is not None:
            grad_acc = train_batch // micro_batch
            grad_acc //= dp_world_size

        # micro_batch_per_gpu needs to be set
        elif train_batch is not None and grad_acc is not None:
            micro_batch = train_batch // dp_world_size
            micro_batch //= grad_acc

        # train_batch_size needs to be set
        elif micro_batch is not None and grad_acc is not None:
            train_batch = micro_batch * grad_acc
            train_batch *= dp_world_size

        # gradient_accumulation_steps and micro_batch_per_gpus is set
        elif train_batch is not None:
            grad_acc = 1
            micro_batch = train_batch // dp_world_size

        # train_batch_size and gradient_accumulation_step is set
        elif micro_batch is not None:
            train_batch = micro_batch * dp_world_size
            grad_acc = 1

        # either none of the three parameters are provided or just gradient_accumulation_step is provided
        else:
            assert (
                False
            ), "Either train_batch_size or micro_batch_per_gpu needs to be provided"
        return int(train_batch), int(micro_batch), int(grad_acc)

    @staticmethod
    def check_batch_parameters(dp_world_size, train_batch, micro_batch, grad_acc):

        assert (
            train_batch > 0
        ), f"Train batch size: {train_batch} has to be greater than 0"

        assert (
            micro_batch > 0
        ), f"Micro batch size per gpu: {micro_batch} has to be greater than 0"

        assert (
            grad_acc > 0
        ), f"Gradient accumulation steps: {grad_acc} has to be greater than 0"

        assert train_batch == micro_batch * grad_acc * dp_world_size, (
            f"Check batch related parameters. train_batch_size is not equal"
            " to micro_batch_per_gpu * gradient_acc_step * world_size \n"
            f"{train_batch} != {micro_batch} * {grad_acc} * {dp_world_size}"
        )

    def calculate_derived(self):
        """
        Derives additional configuration values necessary for training from the current config
        """

        # wandb
        # sets a unique wandb group
        if self.wandb_group is None:
            # if none is defined a uuid is set for the run
            self.wandb_group = shortuuid.uuid()

        # number of gpus
        # Get number of GPUs param or hostfile to determine train_batch_size
        global_num_gpus = getattr(self, "global_num_gpus", None)
        if global_num_gpus is None:
            if self.hostfile is not None or os.path.exists(DLTS_HOSTFILE):
                hostfile_path = self.hostfile or DLTS_HOSTFILE
                resources = obtain_resource_pool(
                    hostfile_path, self.include or "", self.exclude or ""
                )
                if self.num_nodes is not None and self.num_nodes > 0:
                    resources = {
                        k: resources[k]
                        for k in list(resources.keys())[: self.num_nodes]
                    }
                global_num_gpus = sum(map(len, resources.values()))
                if self.num_gpus is not None and self.num_gpus > 0:
                    global_num_gpus = self.num_gpus * len(resources)
            else:
                global_num_gpus = torch.cuda.device_count()
            self.update_value("global_num_gpus", global_num_gpus)

        logging.info(
            self.__class__.__name__
            + ".calculate_derived() "
            + f"Total number of GPUs determined to be: {global_num_gpus}"
        )

        # get world size in the model/pipe parallel case, the actual `world size` deepspeed uses is the size of the
        # data-parallel group, or (num_gpus / mp_size) / pp_size
        pp_size = self.pipe_parallel_size
        pp_size = pp_size if pp_size >= 1 else 1
        mp_size = self.model_parallel_size
        mp_size = mp_size if mp_size >= 1 else 1
        self.update_value("model_parallel_size", mp_size)

        # pp_size and mp_size are only used here to compute dp world size and nowhere else.
        dp_world_size = (global_num_gpus / pp_size) / mp_size
        if not (dp_world_size % 1 == 0):
            error_message = (
                self.__class__.__name__
                + ".calculate_derived() "
                + f"(global_num_gpus / pp_size) / mp_size [({global_num_gpus} / {pp_size}) / {mp_size}] must be a whole number"
            )
            logging.error(error_message)
            raise AssertionError(error_message)

        # Automatically derive train_batch_size = train_micro_batch_size_per_gpu*global_num_gpus*gradient_accumulation_steps
        (
            train_batch_size,
            train_micro_batch_size_per_gpu,
            gradient_accumulation_steps,
        ) = self.calculate_batch_parameters(
            dp_world_size=dp_world_size,
            train_batch=self.train_batch_size,
            micro_batch=self.train_micro_batch_size_per_gpu,
            grad_acc=self.gradient_accumulation_steps,
        )
        self.check_batch_parameters(
            dp_world_size=dp_world_size,
            train_batch=train_batch_size,
            micro_batch=train_micro_batch_size_per_gpu,
            grad_acc=gradient_accumulation_steps,
        )
        self.update_values(
            {
                # batch size params
                "train_batch_size": train_batch_size,
                "train_micro_batch_size_per_gpu": train_micro_batch_size_per_gpu,
                "gradient_accumulation_steps": gradient_accumulation_steps,
                "batch_size": train_micro_batch_size_per_gpu,
                # duplicate items
                "gas": self.gradient_accumulation_steps,
                "clip_grad": self.gradient_clipping,
            }
        )

        # derive precision
        if (self.fp16 or {}).get("type", self.precision) == "bfloat16":
            self.update_value("precision", "bfloat16")
        elif (self.fp16 or {}).get("enabled", False):
            self.update_value("precision", "fp16")
        else:
            self.update_value("precision", "fp32")

        # zero optimization
        if self.zero_optimization is None:
            self.zero_optimization = copy.deepcopy(
                ZERO_DEFAULTS
            )  # a dict is overwritten and not updated key by key
        self.update_values(
            {
                "zero_stage": self.zero_optimization.get(
                    "stage", ZERO_DEFAULTS["stage"]
                ),
                "zero_reduce_scatter": self.zero_optimization.get(
                    "reduce_scatter", ZERO_DEFAULTS["reduce_scatter"]
                ),
                "zero_contiguous_gradients": self.zero_optimization.get(
                    "contiguous_gradients", ZERO_DEFAULTS["contiguous_gradients"]
                ),
                "zero_reduce_bucket_size": self.zero_optimization.get(
                    "reduce_bucket_size", ZERO_DEFAULTS["reduce_bucket_size"]
                ),
                "zero_allgather_bucket_size": self.zero_optimization.get(
                    "allgather_bucket_size", ZERO_DEFAULTS["allgather_bucket_size"]
                ),
            }
        )

        # optimizer and scheduler
        opt_params = self.optimizer or {
            "type": OPT_DEFAULT,
            "params": OPT_PARAMS_DEFAULTS,
        }
        self.update_values(
            {
                "optimizer_type": opt_params.get("type", OPT_DEFAULT),
                "lr": opt_params["params"].get("lr", OPT_PARAMS_DEFAULTS["lr"]),
            }
        )

        if self.optimizer_type.lower() == "onebitadam":
            # onebitadam needs to instantiated by deepspeed, and so we need to pass deepspeed scheduler args
            # for all other optimizers, the scheduling is handled by NeoX
            self.scheduler = {
                "type": "WarmupDecayLR",  # for now this is the only ds scheduler offering decay
                "params": {
                    "warmup_min_lr": 0,
                    "warmup_max_lr": self.lr,
                    "warmup_num_steps": int(self.train_iters * self.warmup),
                    "total_num_steps": self.lr_decay_iters or self.train_iters,
                },
            }

        # Fp16 loss scaling.
        self.update_value("dynamic_loss_scale", self.loss_scale is None)

        # Update 'is pipe parallel' flag
        # if we set pipe_parallel_size to 0 or 1, GPT2ModelPipe.to_sequential() is called, and we run training with
        # the sequential model without the PipelineModule wrapper to avoid the overhead it incurs
        self.update_value("is_pipe_parallel", self.pipe_parallel_size >= 1)

        # Attention config
        if self.attention_config is None:
            self.update_value("attention_config", [[["global"], self.num_layers]])
        self.update_value(
            "attention_config",
            expand_attention_types(self.attention_config, self.num_layers),
        )
        assert (
            len(self.attention_config) == self.num_layers
        ), "Length of attention config list must equal num_layers"
        for item in self.attention_config:
            assert (
                item in ATTENTION_TYPE_CHOICES
            ), f"Attention type {item} not recognized"
        if "gmlp" in self.attention_config or "amlp" in self.attention_config:
            assert (
                not self.partition_activations
            ), "GMLP Blocks are not compatible with partition activations"

        # Sparsity config
        if self.sparsity_config is None:
            # Can't have a default value as an empty dict so need to set it here
            self.update_value("sparsity_config", {})

        # Adding equal dataset weights if none are provided
        if self.train_data_paths and (self.train_data_weights is None):
            self.train_data_weights = [1.0] * len(self.train_data_paths)
        if self.valid_data_paths and (self.valid_data_weights is None):
            self.valid_data_weights = [1.0] * len(self.valid_data_paths)
        if self.test_data_paths and (self.test_data_weights is None):
            self.test_data_weights = [1.0] * len(self.test_data_paths)

    ############################################################################################################################
    # start of validation functions

    @classmethod
    def validate_keys(cls):
        """
        test that there are no duplicate arguments
        """
        source_classes = list(cls.__bases__)
        defined_properties = dict()

        for source_class in source_classes:
            source_vars = list(source_class.__dataclass_fields__)
            for item in source_vars:
                if item in defined_properties.keys():
                    logging.error(
                        f"({cls.__name__}) duplicate of item: {item}, in class {source_class.__name__} and {defined_properties[item]}"
                    )
                    return False
                else:
                    defined_properties[item] = source_class.__name__
        return True

    def validate_values(self):
        # the current codebase assumes running with deepspeed only
        if not self.deepspeed:
            return False

        # learning rate
        if self.lr is None:
            error_message = self.__class__.__name__ + ".validate_values() lr is None"
            logging.error(error_message)
            raise ValueError(error_message)
            return False

        # required arguments
        required_args = [
            "num_layers",
            "hidden_size",
            "num_attention_heads",
            "max_position_embeddings",
        ]
        for req_arg in required_args:
            if getattr(self, req_arg) is None:
                error_message = (
                    self.__class__.__name__
                    + ".validate_values() "
                    + req_arg
                    + " is None."
                )
                logging.error(error_message)
                raise ValueError(error_message)
                return False

        # Checks.
        if self.hidden_size % self.num_attention_heads != 0:
            error_message = (
                self.__class__.__name__
                + ".validate_values() hidden_size must be divisable by num_attention_heads"
            )
            logging.error(error_message)
            raise ValueError(error_message)
            return False

        if self.seq_length is not None:
            if not (self.max_position_embeddings >= self.seq_length):
                error_message = (
                    self.__class__.__name__
                    + ".validate_values() max_position_embeddings must be bigger or equal seq_length"
                )
                logging.error(error_message)
                raise ValueError(error_message)
                return False

        if not (self.min_lr <= self.lr):
            error_message = (
                self.__class__.__name__
                + ".validate_values() min_lr must be smaller or equal lr"
            )
            logging.error(error_message)
            raise ValueError(error_message)
            return False

        if self.save is not None and self.save_interval is None:
            error_message = (
                self.__class__.__name__
                + ".validate_values() save_interval must be defined if save is defined"
            )
            logging.error(error_message)
            raise ValueError(error_message)
            return False

        # Parameters sharing does not work with torch DDP.
        if (self.num_unique_layers is not None) and (self.num_layers is not None):

            if not (self.num_unique_layers <= self.num_layers):
                error_message = (
                    self.__class__.__name__
                    + ".validate_values() num-unique-layers must be smaller or equal num_layers"
                )
                logging.error(error_message)
                raise ValueError(error_message)
                return False

            if not (self.num_layers % self.num_unique_layers == 0):
                error_message = (
                    self.__class__.__name__
                    + ".validate_values() num-layers should be divisible by num-unique-layers"
                )
                logging.error(error_message)
                raise ValueError(error_message)
                return False

        if self.fp16_lm_cross_entropy and self.precision != "fp16":
            error_message = (
                self.__class__.__name__
                + ".validate_values() lm cross entropy in fp16 only support in fp16 mode."
            )
            logging.error(error_message)
            raise ValueError(error_message)
            return False

        # assert that if one of train/test/valid_data_path are provided, data_path should not be
        has_separate_path = [
            data_path is not None
            for data_path in [
                self.train_data_paths,
                self.valid_data_paths,
                self.test_data_paths,
            ]
        ]
        if all(has_separate_path):
            assert self.data_path is None, (
                "Please provide *either* `data_path` or `train/valid/test_data_path` "
                "in args "
            )

        # assert that if one of train/test/valid_data_path are provided, all should be
        assert_error_mess = (
            "One or more of train/valid/test data_path are not provided:\n\t"
        )
        assert_error_mess += "\n\t".join(
            [
                f"{name} data paths: {data_path},"
                for name, data_path in [
                    ["train", self.train_data_paths],
                    ["valid", self.valid_data_paths],
                    ["test", self.test_data_paths],
                ]
            ]
        )
        assert any(has_separate_path) == all(has_separate_path), assert_error_mess

        # assert that if train / valid / test data path(s) and weights are provided, that the paths and the weights should be equal length
        if self.train_data_paths is not None:
            assert len(self.train_data_paths) == len(self.train_data_weights)
        if self.valid_data_paths is not None:
            assert len(self.valid_data_paths) == len(self.valid_data_weights)
        if self.test_data_paths is not None:
            assert len(self.test_data_paths) == len(self.test_data_weights)

        return True

    def validate_types(self):
        """
        At runtime, checks types are actually the type specified.
        """
        for field_name, field_def in self.__dataclass_fields__.items():

            actual_value = getattr(self, field_name)
            if actual_value is None:
                continue  # we allow for some values not to be configured

            actual_type = type(actual_value)
            if actual_type != field_def.type:
                if (
                    actual_type == int and field_def.type == float
                ):  # floats should be able to be configured as ints
                    continue

                # for typing.Literal (i.e a list of choices) - checks that actual value is in accepted values
                elif field_def.type.__origin__ == Literal:
                    accepted_values = field_def.type.__args__
                    if actual_value in accepted_values:
                        continue
                    elif type(actual_value) == str:
                        # case insensitive checking
                        lowercase_accepted_values = [
                            i.lower() for i in accepted_values if isinstance(i, str)
                        ]
                        if actual_value.lower() in lowercase_accepted_values:
                            continue
                    logging.error(
                        self.__class__.__name__
                        + ".validate_types() "
                        + f"{field_name}: '{actual_value}' Not in accepted values: '{accepted_values}'"
                    )
                    return False

                logging.error(
                    self.__class__.__name__
                    + ".validate_types() "
                    + f"{field_name}: '{actual_type}' instead of '{field_def.type}'"
                )
                return False

        # validate deepspeed dicts
        for field_name in ["optimizer", "scheduler"]:
            value = getattr(self, field_name)
            if isinstance(
                value, dict
            ):  # dict is checked above, only fields are checked here
                if "type" in value:
                    if not isinstance(value["type"], str):
                        logging.error(
                            self.__class__.__name__
                            + ".validate_types() "
                            + f"{field_name}: key 'type' must be a string"
                        )
                        return False
                else:
                    logging.error(
                        self.__class__.__name__
                        + ".validate_types() "
                        + f"{field_name}: must contain key 'type'"
                    )
                    return False
                if "params" in value:
                    if not isinstance(value["params"], dict):
                        logging.error(
                            self.__class__.__name__
                            + ".validate_types() "
                            + f"{field_name}: key 'params' must be a dict"
                        )
                        return False
                else:
                    logging.error(
                        self.__class__.__name__
                        + ".validate_types() "
                        + f"{field_name}: must contain key 'params'"
                    )
                    return False

        for field_name in ["fp16", "amp", "flops_profiler"]:
            value = getattr(self, field_name)
            if isinstance(value, dict):
                if not "enabled" in value:
                    error_message = (
                        self.__class__.__name__
                        + ".validate_types() "
                        + f"{field_name}: must contain key 'enabled'"
                    )
                    logging.error(error_message)
                    return False

        return True