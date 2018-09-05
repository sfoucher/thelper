import json
import logging
import os
import time
import platform
from copy import deepcopy
from abc import abstractmethod

import torch
import torch.optim

import tensorboardX
import cv2 as cv

import thelper.utils

logger = logging.getLogger(__name__)


def load_trainer(session_name, save_dir, config, model, loaders, ckptdata=None):
    if "trainer" not in config or not config["trainer"]:
        raise AssertionError("config missing 'trainer' field")
    trainer_config = config["trainer"]
    if "type" not in trainer_config or not trainer_config["type"]:
        raise AssertionError("trainer config missing 'type' field")
    trainer_type = thelper.utils.import_class(trainer_config["type"])
    if "params" not in trainer_config:
        raise AssertionError("trainer config missing 'params' field")
    params = thelper.utils.keyvals2dict(trainer_config["params"])
    return trainer_type(session_name, save_dir, model, loaders, trainer_config, ckptdata=ckptdata, **params)


class Trainer:

    def __init__(self, session_name, save_dir, model, loaders, config, ckptdata=None):
        if not model or not loaders or not config:
            raise AssertionError("missing input args")
        train_loader, valid_loader, test_loader = loaders
        if not (train_loader or valid_loader or test_loader):
            raise AssertionError("must provide at least one loader with available data")
        self.logger = thelper.utils.get_class_logger()
        self.name = session_name
        self.train_loader = train_loader
        self.valid_loader = valid_loader
        self.test_loader = test_loader
        if "epochs" not in config or not config["epochs"] or int(config["epochs"]) <= 0:
            raise AssertionError("bad trainer config epoch count")
        if train_loader:
            self.epochs = int(config["epochs"])
            # no need to load optimization stuff if not training (i.e. no train_loader)
            # loading optimization stuff later since model needs to be on correct device
            if "optimization" not in config or not config["optimization"]:
                raise AssertionError("trainer config missing 'optimization' field")
            self.optimization_config = config["optimization"]
        else:
            self.epochs = 1
            self.logger.info("no training data provided, will run a single epoch on valid/test data")
        self.save_freq = int(config["save_freq"]) if "save_freq" in config else 1
        self.save_dir = save_dir
        self.checkpoint_dir = os.path.join(self.save_dir, "checkpoints")
        self.config_backup_path = os.path.join(self.save_dir, "config.json")  # this file is created in cli.get_save_dir
        if not os.path.exists(self.checkpoint_dir):
            os.mkdir(self.checkpoint_dir)
        self.use_tbx = False
        if "use_tbx" in config:
            self.use_tbx = thelper.utils.str2bool(config["use_tbx"])
        writer_paths = [None, None, None]
        if self.use_tbx:
            self.tbx_root_dir = os.path.join(self.save_dir, "tbx_logs", self.name)
            if not os.path.exists(self.tbx_root_dir):
                os.makedirs(self.tbx_root_dir)
            timestr = time.strftime("%Y%m%d-%H%M%S")
            train_foldername = "train-%s-%s" % (platform.node(), timestr)
            valid_foldername = "valid-%s-%s" % (platform.node(), timestr)
            test_foldername = "test-%s-%s" % (platform.node(), timestr)
            foldernames = [train_foldername, valid_foldername, test_foldername]
            for idx, (loader, foldername) in enumerate(zip(loaders, foldernames)):
                if loader:
                    tbx_dir = os.path.join(self.tbx_root_dir, foldername)
                    if os.path.exists(tbx_dir):
                        raise AssertionError("tbx session paths should be unique")
                    writer_paths[idx] = tbx_dir
            self.logger.debug("tensorboard init : tensorboard --logdir %s --port <your_port>" % self.tbx_root_dir)
        self.train_writer_path, self.valid_writer_path, self.test_writer_path = writer_paths
        self.model = model
        self.optimizer = None
        self.scheduler = None
        self.config = config
        self.default_dev = "cpu"
        if torch.cuda.is_available():
            self.default_dev = "cuda:0"
        devices = [None] * 3
        for idx, field in enumerate(["train_device", "valid_device", "test_device"]):
            device_str = str(config[field]) if field in config and config[field] else self.default_dev
            if not torch.cuda.is_available() and "cuda" in device_str:
                raise AssertionError("cuda not available (according to pytorch), cannot use in '%s' field" % field)
            curr_devices = device_str.split(",")
            cuda_device_idxs = []
            for dev_idx, device in enumerate(curr_devices):
                if "cuda" not in device and "cpu" not in device:
                    raise AssertionError("unknown device type '%s' for field '%s'" % (device, field))
                elif device == "cpu":
                    if len(curr_devices) > 1:
                        raise AssertionError("cannot combine cpu with other devices in field '%s'" % field)
                    else:
                        devices[idx] = "cpu"
                        break
                elif device == "cuda":
                    if len(curr_devices) > 1:
                        raise AssertionError("must specify device index (e.g. 'cuda:0') if combining devices in '%s'" % field)
                    else:
                        devices[idx] = self.default_dev
                        break
                elif "cuda:" not in device:
                    raise AssertionError("expecting cuda device format to be 'cuda:X' (where X is device index)")
                cuda_dev_count = torch.cuda.device_count()
                cuda_dev_str = device.rsplit(":", 1)[-1]
                if cuda_dev_str == "all":
                    if len(curr_devices) > 1:
                        raise AssertionError("use of 'cuda:all' must not be combined with other devices")
                    if cuda_dev_count == 1:
                        devices[idx] = self.default_dev
                        break
                    else:
                        devices[idx] = []  # will be interpreted as 'use all cuda devices' later
                        break
                else:
                    cuda_dev_idx = int(device.rsplit(":", 1)[-1])
                    if cuda_dev_idx >= cuda_dev_count:
                        raise AssertionError("cuda device '%s' out of range (detected device count = %d)" % (device, cuda_dev_count))
                    if len(curr_devices) == 1:
                        devices[idx] = device
                        break
                    else:
                        cuda_device_idxs.append(cuda_dev_idx)
            if devices[idx] is None:
                devices[idx] = cuda_device_idxs
        self.train_dev, self.valid_dev, self.test_dev = tuple(devices)
        if "loss" not in config or not config["loss"]:
            raise AssertionError("trainer config missing 'loss' field")
        self.loss = self._load_loss(config["loss"], next((loader for loader in loaders if loader is not None)).dataset)
        if hasattr(self.loss, "summary"):
            self.loss.summary()
        if "metrics" not in config or not config["metrics"]:
            raise AssertionError("trainer config missing 'metrics' field")
        metrics = self._load_metrics(config["metrics"])
        for metric_name, metric in metrics.items():
            if hasattr(metric, "summary"):
                logger.info("parsed metric category '%s'" % metric_name)
                metric.summary()
        # later, we could use different metrics for each usage type
        self.train_metrics = deepcopy(metrics)
        self.valid_metrics = deepcopy(metrics)
        self.test_metrics = deepcopy(metrics)
        if "monitor" not in config or not config["monitor"]:
            raise AssertionError("missing 'monitor' field for trainer config")
        self.monitor = config["monitor"]
        if self.monitor not in self.train_metrics:
            raise AssertionError("monitored metric with name '%s' should be declared in config 'metrics' field" % self.monitor)
        self.monitor_goal = self.train_metrics[self.monitor].goal()
        self.monitor_best = None
        if self.monitor_goal == thelper.optim.Metric.minimize:
            self.monitor_best = thelper.optim.Metric.maximize
        elif self.monitor_goal == thelper.optim.Metric.maximize:
            self.monitor_best = thelper.optim.Metric.minimize
        else:
            raise AssertionError("monitored metric does not return proper optimization goal")
        train_logger_path = os.path.join(self.save_dir, "logs", "train.log")
        train_logger_format = logging.Formatter("[%(asctime)s - %(process)s] %(levelname)s : %(message)s")
        train_logger_fh = logging.FileHandler(train_logger_path)
        train_logger_fh.setFormatter(train_logger_format)
        self.logger.addHandler(train_logger_fh)
        self.logger.info("created training log for session '%s'" % session_name)
        self.current_lr = self._get_lr()  # for debug/display purposes only
        if ckptdata is not None:
            self.monitor_best = ckptdata["monitor_best"]
            self.model.load_state_dict(ckptdata["state_dict"])
            self.optimizer_state = ckptdata["optimizer"]
            self.current_iter = ckptdata["iter"] if "iter" in ckptdata else 0
            self.current_epoch = ckptdata["epoch"]
            self.outputs = ckptdata["outputs"]
        else:
            self.optimizer_state = None
            self.current_iter = 0
            self.current_epoch = 0
            self.outputs = {}

    def _upload_model(self, model, dev):
        if isinstance(dev, list):
            if len(dev) == 0:
                return torch.nn.DataParallel(model).cuda()
            else:
                return torch.nn.DataParallel(model, device_ids=dev).cuda(dev[0])
        else:
            return model.to(dev)

    def _upload_tensor(self, tensor, dev):
        if isinstance(dev, list):
            if len(dev) == 0:
                return tensor.cuda()
            else:
                return tensor.cuda(dev[0])
        else:
            return tensor.to(dev)

    def _load_loss(self, config, dataset):
        if "type" not in config or not config["type"]:
            raise AssertionError("loss config missing 'type' field")
        loss_type = thelper.utils.import_class(config["type"])
        if "params" not in config:
            raise AssertionError("loss config missing 'params' field")
        params = thelper.utils.keyvals2dict(config["params"])
        if isinstance(self.model.task, thelper.tasks.Classification) and "weight_classes" in config:
            weight_classes = thelper.utils.str2bool(config["weight_classes"])
            if weight_classes:
                weight_param_name = "weight"
                if "weight_param_name" in config:
                    weight_param_name = config["weight_param_name"]
                weight_param_pass_tensor = True
                if "weight_param_pass_tensor" in config:
                    weight_param_pass_tensor = thelper.utils.str2bool(config["weight_param_pass_tensor"])
                weight_distribution = "uniform"
                if "weight_distribution" in config:
                    weight_distribution = config["weight_distribution"]
                    if not isinstance(weight_distribution, str) or (
                        weight_distribution != "uniform" and "root" not in weight_distribution):
                        raise AssertionError("unexpected weight distribution strategy")
                weight_max = float("inf")
                if "weight_max" in config:
                    weight_max = float(config["weight_max"])
                weight_norm = True
                if "weight_norm" in config:
                    weight_norm = thelper.utils.str2bool(config["weight_norm"])
                class_sizes = self.model.task.get_class_sizes(dataset.samples)
                tot_samples = sum(class_sizes.values())
                class_weights = None
                pow = 1
                if "root" in weight_distribution:
                    pow = 1.0 / int(weight_distribution.split("root", 1)[
                                        1])  # will be the inverse power to use for rooting weights
                class_weights = {label: (size / tot_samples) ** pow for label, size in class_sizes.items()}
                class_weights = {label: max(class_weights.values()) / weight for label, weight in class_weights.items()}
                class_weights = {label: min(weight, weight_max) for label, weight in class_weights.items()}
                if weight_norm:
                    avg_weight = sum(class_weights.values()) / len(class_weights)
                    class_weights = {label: weight / avg_weight for label, weight in class_weights.items()}
                if weight_param_pass_tensor:
                    params[weight_param_name] = self._upload_tensor(torch.FloatTensor(list(class_weights.values())), dev=self.train_dev)
                else:
                    params[weight_param_name] = class_weights
        loss = loss_type(**params)
        return loss

    @staticmethod
    def _load_metrics(config):
        if not isinstance(config, dict):
            raise AssertionError("metrics config should be provided as dict")
        metrics = {}
        for name, metric_config in config.items():
            if "type" not in metric_config or not metric_config["type"]:
                raise AssertionError("metric config missing 'type' field")
            metric_type = thelper.utils.import_class(metric_config["type"])
            if "params" not in metric_config:
                raise AssertionError("metric config missing 'params' field")
            params = thelper.utils.keyvals2dict(metric_config["params"])
            metric = metric_type(**params)
            goal = getattr(metric, "goal", None)
            if not callable(goal):
                raise AssertionError("expected metric to define 'goal' based on parent interface")
            metrics[name] = metric
        return metrics

    @staticmethod
    def _load_optimization(model, config):
        if not isinstance(config, dict):
            raise AssertionError("optimization config should be provided as dict")
        if "optimizer" not in config or not config["optimizer"]:
            raise AssertionError("optimization config missing 'optimizer' field")
        optimizer_config = config["optimizer"]
        if "type" not in optimizer_config or not optimizer_config["type"]:
            raise AssertionError("optimizer config missing 'type' field")
        optimizer_type = thelper.utils.import_class(optimizer_config["type"])
        optimizer_params = thelper.utils.keyvals2dict(
            optimizer_config["params"]) if "params" in optimizer_config else None
        optimizer = optimizer_type(filter(lambda p: p.requires_grad, model.parameters()), **optimizer_params)
        scheduler = None
        if "scheduler" in config and config["scheduler"]:
            scheduler_config = config["scheduler"]
            if "type" not in scheduler_config or not scheduler_config["type"]:
                raise AssertionError("scheduler config missing 'type' field")
            scheduler_type = thelper.utils.import_class(scheduler_config["type"])
            scheduler_params = thelper.utils.keyvals2dict(
                scheduler_config["params"]) if "params" in scheduler_config else None
            scheduler = scheduler_type(optimizer, **scheduler_params)
        return optimizer, scheduler

    def train(self):
        if not self.train_loader:
            raise AssertionError("missing training data, invalid loader!")
        model = self._upload_model(self.model, self.train_dev)
        self.optimizer, self.scheduler = self._load_optimization(model, self.optimization_config)
        if self.optimizer_state is not None:
            self.optimizer.load_state_dict(self.optimizer_state)
        start_epoch = self.current_epoch + 1
        train_writer, valid_writer, test_writer = None, None, None
        for epoch in range(start_epoch, self.epochs + 1):
            if self.scheduler:
                self.scheduler.step(epoch=(epoch - 1))  # epoch idx is 1-based, scheduler expects 0-based
                self.current_lr = self.scheduler.get_lr()[0]  # for debug/display purposes only
                self.logger.info("learning rate at %.8f" % self.current_lr)
            model.train()
            if self.use_tbx and not train_writer:
                train_writer = tensorboardX.SummaryWriter(log_dir=self.train_writer_path, comment=self.name)
                setattr(train_writer, "path", self.train_writer_path)  # for external usage, if needed
                setattr(train_writer, "prefix", "train")  # to prefix data added to tbx logs (if needed)
            for metric in self.train_metrics.values():
                if hasattr(metric, "set_max_accum") and callable(metric.set_max_accum):
                    metric.set_max_accum(len(self.train_loader))  # used to make scalar metric evals smoother between epochs
                if metric.needs_reset():
                    metric.reset()  # if a metric needs to be reset between two epochs, do it here
            loss, self.current_iter = self._train_epoch(model, epoch, self.current_iter, self.train_dev, self.optimizer,
                                                        self.train_loader, self.train_metrics, train_writer)
            self._write_epoch_metrics(epoch, self.train_metrics, train_writer)
            train_metric_vals = {metric_name: metric.eval() for metric_name, metric in self.train_metrics.items()}
            result = {"train/loss": loss, "train/metrics": train_metric_vals}
            monitor_type_key = "train/metrics"  # if we cannot run validation, will monitor progression on training metrics
            if self.valid_loader:
                model.eval()
                # here, we reuse the model on the train device, as switching may slow everything down hard
                # todo: run eval in parallel (i.e. at the same time as training?)
                if self.use_tbx and not valid_writer:
                    valid_writer = tensorboardX.SummaryWriter(log_dir=self.valid_writer_path, comment=self.name)
                    setattr(valid_writer, "path", self.valid_writer_path)  # for external usage, if needed
                    setattr(valid_writer, "prefix", "valid")  # to prefix data added to tbx logs (if needed)
                for metric in self.valid_metrics.values():
                    metric.reset()  # force reset here, we always evaluate from a clean state
                self._eval_epoch(model, epoch, self.current_iter, self.valid_dev,
                                 self.valid_loader, self.valid_metrics, valid_writer)
                self._write_epoch_metrics(epoch, self.valid_metrics, valid_writer)
                valid_metric_vals = {metric_name: metric.eval() for metric_name, metric in self.valid_metrics.items()}
                result = {**result, "valid/metrics": valid_metric_vals}
                monitor_type_key = "valid/metrics"  # since validation is available, use that to monitor progression
            new_best = False
            losses = {}
            monitor_vals = {}
            for key, value in result.items():
                if key == "train/metrics":
                    if self.monitor not in value:
                        raise AssertionError("not monitoring required variable '%s' in training metrics" % self.monitor)
                    monitor_vals["train"] = value[self.monitor]
                elif key == "valid/metrics":
                    if self.monitor not in value:
                        raise AssertionError("not monitoring required variable '%s' in validation metrics" % self.monitor)
                    monitor_vals["valid"] = value[self.monitor]
                if (key == monitor_type_key and
                    ((self.monitor_goal == thelper.optim.Metric.minimize and value[self.monitor] < self.monitor_best) or
                     (self.monitor_goal == thelper.optim.Metric.maximize and value[self.monitor] > self.monitor_best))):
                    self.monitor_best = value[self.monitor]
                    new_best = True
                if key == "train/loss":
                    losses["train"] = value
                elif key == "valid/loss":
                    losses["valid"] = value
                if not isinstance(value, dict):
                    self.logger.debug(" epoch {} result =>  {}: {}".format(epoch, str(key), value))
                else:
                    for subkey, subvalue in value.items():
                        self.logger.debug(" epoch {} result =>  {}:{}: {}".format(epoch, str(key), str(subkey), subvalue))
            if not monitor_vals or not losses:
                raise AssertionError("training/validation did not produce required losses & monitoring variable '%s'" % self.monitor)
            self.outputs[epoch] = result
            if new_best or (epoch % self.save_freq) == 0:
                self._save(epoch, save_best=new_best)
        self.logger.info("training for session '%s' done" % self.name)
        if self.test_loader:
            # reload 'best' model checkpoint on cpu (will remap to current device setup)
            filename_best = os.path.join(self.checkpoint_dir, "ckpt.best.pth")
            self.logger.info("loading best model & initializing final test run")
            ckptdata = torch.load(filename_best, map_location="cpu")
            if self.config_backup_path and os.path.exists(self.config_backup_path):
                with open(self.config_backup_path, "r") as fd:
                    config_backup = json.load(fd)
                    if config_backup["model"] != ckptdata["config"]["model"]:
                        raise AssertionError("could not load compatible best checkpoint to run test eval")
            self.model.load_state_dict(ckptdata["state_dict"])
            best_epoch = ckptdata["epoch"]
            best_iter = ckptdata["iter"] if "iter" in ckptdata else None
            model = self._upload_model(self.model, self.test_dev)
            model.eval()
            if self.use_tbx and not test_writer:
                test_writer = tensorboardX.SummaryWriter(log_dir=self.test_writer_path, comment=self.name)
                setattr(test_writer, "path", self.test_writer_path)  # for external usage, if needed
                setattr(test_writer, "prefix", "test")  # to prefix data added to tbx logs (if needed)
            for metric in self.test_metrics.values():
                metric.reset()  # force reset here, we always evaluate from a clean state
            self._eval_epoch(model, best_epoch, best_iter, self.test_dev,
                             self.test_loader, self.test_metrics, test_writer)
            self._write_epoch_metrics(best_epoch, self.test_metrics, test_writer)
            test_metric_vals = {metric_name: metric.eval() for metric_name, metric in self.test_metrics.items()}
            self.outputs[best_epoch] = {**ckptdata["outputs"], "test/metrics": test_metric_vals}
            for key, value in self.outputs[best_epoch].items():
                if not isinstance(value, dict):
                    self.logger.debug(" final result =>  {}: {}".format(str(key), value))
                else:
                    for subkey, subvalue in value.items():
                        self.logger.debug(" final result =>  {}:{}: {}".format(str(key), str(subkey), subvalue))
            self.logger.info("evaluation for session '%s' done" % self.name)

    def eval(self):
        if not self.valid_loader and not self.test_loader:
            raise AssertionError("missing validation/test data, invalid loaders!")
        result = {}
        valid_writer, test_writer = None, None
        if self.test_loader:
            model = self._upload_model(self.model, self.test_dev)
            model.eval()
            if self.use_tbx and not test_writer:
                test_writer = tensorboardX.SummaryWriter(log_dir=self.test_writer_path, comment=self.name)
                setattr(test_writer, "path", self.test_writer_path)  # for external usage, if needed
                setattr(test_writer, "prefix", "test")  # to prefix data added to tbx logs (if needed)
            for metric in self.test_metrics.values():
                metric.reset()  # force reset here, we always evaluate from a clean state
            self._eval_epoch(model, self.current_epoch, self.current_iter, self.test_dev,
                             self.test_loader, self.test_metrics, test_writer)
            self._write_epoch_metrics(self.current_epoch, self.test_metrics, test_writer)
            test_metric_vals = {metric_name: metric.eval() for metric_name, metric in self.test_metrics.items()}
            result = {**result, **test_metric_vals}
        elif self.valid_loader:
            model = self._upload_model(self.model, self.valid_dev)
            model.eval()
            if self.use_tbx and not valid_writer:
                valid_writer = tensorboardX.SummaryWriter(log_dir=self.valid_writer_path, comment=self.name)
                setattr(valid_writer, "path", self.valid_writer_path)  # for external usage, if needed
                setattr(valid_writer, "prefix", "valid")  # to prefix data added to tbx logs (if needed)
            for metric in self.valid_metrics.values():
                metric.reset()  # force reset here, we always evaluate from a clean state
            self._eval_epoch(model, self.current_epoch, self.current_iter, self.valid_dev,
                             self.valid_loader, self.valid_metrics, valid_writer)
            self._write_epoch_metrics(self.current_epoch, self.valid_metrics, valid_writer)
            valid_metric_vals = {metric_name: metric.eval() for metric_name, metric in self.valid_metrics.items()}
            result = {**result, **valid_metric_vals}
        for key, value in result.items():
            if not isinstance(value, dict):
                self.logger.debug(" final result =>  {}: {}".format(str(key), value))
            else:
                for subkey, subvalue in value.items():
                    self.logger.debug(" final result =>  {}:{}: {}".format(str(key), str(subkey), subvalue))
        self.outputs[self.current_epoch] = result
        self.logger.info("evaluation for session '%s' done" % self.name)

    @abstractmethod
    def _train_epoch(self, model, epoch, iter, dev, optimizer, loader, metrics, writer=None):
        raise NotImplementedError

    @abstractmethod
    def _eval_epoch(self, model, epoch, iter, dev, loader, metrics, writer=None):
        raise NotImplementedError

    def _get_lr(self):
        if self.optimizer:
            for param_group in self.optimizer.param_groups:
                if "lr" in param_group:
                    return param_group["lr"]
        return 0.0

    def _write_epoch_metrics(self, epoch, metrics, writer):
        if not self.use_tbx or writer is None:
            return
        for metric_name, metric in metrics.items():
            if metric.is_scalar():
                writer.add_scalar("epoch/%s" % metric_name, metric.eval(), epoch)
            if hasattr(metric, "get_tbx_image") and callable(metric.get_tbx_image):
                img = metric.get_tbx_image()
                if img is not None:
                    writer.add_image(writer.prefix + "/%s" % metric_name, img, epoch)
                    raw_filename = "%s-%s-%04d.png" % (writer.prefix, metric_name, epoch)
                    raw_filepath = os.path.join(writer.path, raw_filename)
                    cv.imwrite(raw_filepath, img[..., [2, 1, 0]])
            if hasattr(metric, "get_tbx_text") and callable(metric.get_tbx_text):
                txt = metric.get_tbx_text()
                if txt:
                    writer.add_text(writer.prefix + "/%s" % metric_name, txt, epoch)
                    # as backup, save raw text since tensorboardX can fail
                    # see https://github.com/lanpa/tensorboardX/issues/134
                    raw_filename = "%s-%s-%04d.txt" % (writer.prefix, metric_name, epoch)
                    raw_filepath = os.path.join(writer.path, raw_filename)
                    with open(raw_filepath, "w") as fd:
                        fd.write(txt)

    def _save(self, epoch, save_best=False):
        # logically, this should only be called during training (i.e. with a valid optimizer)
        fullconfig = None
        if self.config_backup_path and os.path.exists(self.config_backup_path):
            with open(self.config_backup_path, "r") as fd:
                fullconfig = json.load(fd)
        timestr = time.strftime("%Y%m%d-%H%M%S")
        curr_state = {
            "name": self.name,
            "epoch": epoch,
            "iter": self.current_iter,
            "time": timestr,
            "host": platform.node(),
            "task": self.model.task,
            "outputs": self.outputs[epoch],
            "state_dict": self.model.state_dict(),
            "optimizer": self.optimizer.state_dict(),
            "monitor_best": self.monitor_best,
            "config": fullconfig
        }
        filename = "ckpt.%04d.%s.%s.pth" % (epoch, platform.node(), timestr)
        filename = os.path.join(self.checkpoint_dir, filename)
        torch.save(curr_state, filename)
        if save_best:
            filename_best = os.path.join(self.checkpoint_dir, "ckpt.best.pth")
            torch.save(curr_state, filename_best)
            self.logger.info("saving new best checkpoint @ epoch %d" % epoch)
        else:
            self.logger.info("saving checkpoint @ epoch %d" % epoch)


class ImageClassifTrainer(Trainer):

    def __init__(self, session_name, save_dir, model, loaders, config, ckptdata=None):
        super().__init__(session_name, save_dir, model, loaders, config, ckptdata=ckptdata)
        if not isinstance(self.model.task, thelper.tasks.Classification):
            raise AssertionError("expected task to be classification")
        input_key = self.model.task.get_input_key()
        self.input_keys = input_key if isinstance(input_key, list) else [input_key]
        label_key = self.model.task.get_gt_key()
        self.label_keys = label_key if isinstance(label_key, list) else [label_key]
        self.class_names = self.model.task.get_class_names()
        self.meta_keys = self.model.task.get_meta_keys()
        self.class_idxs_map = self.model.task.get_class_idxs_map()
        metrics = list(self.train_metrics.values()) + list(self.valid_metrics.values()) + list(self.test_metrics.values())
        for metric in metrics:  # check all metrics for classification-specific attributes, and set them
            if hasattr(metric, "set_class_names") and callable(metric.set_class_names):
                metric.set_class_names(self.class_names)

    def _to_tensor(self, sample):
        if not isinstance(sample, dict):
            raise AssertionError("trainer expects samples to come in dicts for key-based usage")
        input, label = None, None
        for key in self.input_keys:
            if key in sample:
                input = sample[key]
                break  # by default, stop after finding first key hit
        for key in self.label_keys:
            if key in sample:
                label = sample[key]
                break  # by default, stop after finding first key hit
        if input is None or label is None:
            raise AssertionError("could not find input or label key in sample dict")
        label_idx = []
        for class_name in label:
            if not isinstance(class_name, str):
                raise AssertionError("expected label to be in str format (task will convert to proper index)")
            if class_name not in self.class_names:
                raise AssertionError("got unexpected label '%s' for a sample (unknown class)" % class_name)
            label_idx.append(self.class_idxs_map[class_name])
        return torch.FloatTensor(input), torch.LongTensor(label_idx)

    def _train_epoch(self, model, epoch, iter, dev, optimizer, loader, metrics, writer=None):
        if not optimizer:
            raise AssertionError("missing optimizer")
        if not loader:
            raise AssertionError("no available data to load")
        if not isinstance(metrics, dict):
            raise AssertionError("expect metrics as dict object")
        total_loss = 0
        epoch_size = len(loader)
        for idx, sample in enumerate(loader):
            input, label = self._to_tensor(sample)
            input = self._upload_tensor(input, dev)
            label = self._upload_tensor(label, dev)
            meta = {key: sample[key] for key in self.meta_keys}
            optimizer.zero_grad()
            pred = model(input)
            loss = self.loss(pred, label)
            loss.backward()
            optimizer.step(epoch)
            total_loss += loss.item()
            if iter is not None:
                iter += 1
            for metric in metrics.values():
                metric.accumulate(pred.cpu(), label.cpu(), meta=meta)
            self.logger.info(
                "train epoch: {}   iter: {}   batch: {}/{} ({:.0f}%)   loss: {:.6f}   {}: {:.2f}".format(
                    epoch,
                    iter,
                    idx,
                    epoch_size,
                    (idx / epoch_size) * 100.0,
                    loss.item(),
                    self.monitor,
                    metrics[self.monitor].eval()
                )
            )
            if writer and iter is not None:
                writer.add_scalar("iter/loss", loss.item(), iter)
                writer.add_scalar("iter/lr", self.current_lr, iter)
                for metric_name, metric in metrics.items():
                    if metric.is_scalar():  # only useful assuming that scalar metrics are smoothed...
                        writer.add_scalar("iter/%s" % metric_name, metric.eval(), iter)
        if writer:
            writer.add_scalar("epoch/loss", total_loss / epoch_size, epoch)
            writer.add_scalar("epoch/lr", self.current_lr, epoch)
        return total_loss / epoch_size, iter

    def _eval_epoch(self, model, epoch, iter, dev, loader, metrics, writer=None):
        if not loader:
            raise AssertionError("no available data to load")
        with torch.no_grad():
            epoch_size = len(loader)
            for idx, sample in enumerate(loader):
                input, label = self._to_tensor(sample)
                input = self._upload_tensor(input, dev)
                label = self._upload_tensor(label, dev)
                meta = {key: sample[key] for key in self.meta_keys}
                pred = model(input)
                for metric in metrics.values():
                    metric.accumulate(pred.cpu(), label.cpu(), meta=meta)
                self.logger.info(
                    "eval epoch: {}   batch: {}/{} ({:.0f}%)   {}: {:.2f}".format(
                        epoch,
                        idx,
                        epoch_size,
                        (idx / epoch_size) * 100.0,
                        self.monitor,
                        metrics[self.monitor].eval()
                    )
                )
