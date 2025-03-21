import logging
from typing import Any, Dict, List, Union

import lightning.pytorch as pl
import torch
from lightning.pytorch.callbacks import Callback
from torch.optim import Optimizer

STEP_OUTPUT = Union[torch.Tensor, Dict[str, Any]]


class ThresholdBasedPruning(Callback):
    """
    Callback for a very simple pruning strategy using weights magnitude like the following:
        f(wi)= {wi: if |wi| > thresh
                0:  if |wi| ≤ thresh}

    Args:
        threshold (float):
            value below which weights will be pruned
        start_pruning_epoch (int):
            epoch from which to apply the pruning strategy
    """

    def __init__(
        self, threshold: float, start_pruning_epoch: int = 10, *args: Any, **kwargs: Any
    ):
        super().__init__()
        self.threshold = threshold
        self.start_pruning_epoch = start_pruning_epoch

    def on_before_optimizer_step(
        self, trainer: pl.Trainer, pl_module: pl.LightningModule, optimizer: Optimizer
    ) -> None:
        """
        Method called before `optimizer.step()` to zero prune gradients

        Args:
            trainer (pl.Trainer):
                Lightning trainer
            pl_module (pl.LightningModule):
                Lightning module
            optimizer (Optimizer):
                optimizer used for training

        """
        if pl_module.current_epoch >= self.start_pruning_epoch != -1:
            self._zero_pruned_gradients(pl_module)

    def on_train_epoch_end(
        self, trainer: pl.Trainer, pl_module: pl.LightningModule
    ) -> None:
        """
        Method called at the end of a training epoch to prune the model.

        Args:
            trainer (pl.Trainer):
                Lightning trainer
            pl_module (pl.LightningModule):
                Lightning module
        """
        if pl_module.current_epoch >= self.start_pruning_epoch != -1:
            logging.info(
                "======== Pruning iteration"
                f" {self.start_pruning_epoch - pl_module.current_epoch - 1} ========"
            )
            self._prune_model(pl_module)

    def on_fit_end(self, trainer: pl.Trainer, pl_module: pl.LightningModule) -> None:
        """
        Method called when fit ends.

        Args:
            trainer (pl.Trainer):
                Lightning trainer
            pl_module (pl.LightningModule):
                Lightning module
        """
        print("Pruning model...")
        if self.start_pruning_epoch == -1:
            self._prune_model(pl_module)

    def _prune_model(self, pl_module: pl.LightningModule) -> None:
        """
        Method used to zero parameters which magnitude are below `self.threshold`.

        Args:
            pl_module (pl.LightningModule):
                Lightning module
        """
        try:
            # Only prune the student when performing distillation
            pl_module = pl_module.get_submodule("student")
        except AttributeError:
            pass

        for name, param in pl_module.named_parameters():
            if "weight" in name:
                param_mask = torch.abs(param) < self.threshold
                param.data[param_mask] = 0.0

    @staticmethod
    def _zero_pruned_gradients(pl_module: pl.LightningModule) -> None:
        """
        Method used for zeroing gradients of pruned weights

        Args:
            pl_module (pl.LightningModule):
                Lightning module
        """
        for name, param in pl_module.named_parameters():
            if "weight" in name:
                param_data = param.data.cpu()
                param_grad = param.grad.data.cpu()

                dummy_zero_tensor = torch.zeros_like(param_grad)
                param_grad = torch.where(param_data == 0.0, dummy_zero_tensor, param_grad)
                param.grad.data = param_grad.to(pl_module.device)


class SparsityBasedPruning(Callback):
    """
    Callback to achieve a given level of sparsity.

    Given a level of sparsity x it will prune n weights to such that (x * n)%
    of the model weights are pruned.
    """

    def __init__(
        self,
        sparsity_level: float = 0.0,
        layers_to_exclude: List[str] = None,
        *args: Any,
        **kwargs: Any,
    ):
        super().__init__(*args, **kwargs)
        self.sparsity_level = sparsity_level
        self.layers_to_exclude = layers_to_exclude

    def on_fit_end(self, trainer: pl.Trainer, pl_module: pl.LightningModule) -> None:
        """
        Method called when fit ends.

        Args:
            trainer (pl.Trainer):
                Lightning trainer
            pl_module (pl.LightningModule):
                Lightning module
        """
        if self.sparsity_level == 0.0:
            return

        # flatten all tensors and create a huge 1D tensor to get the threshold
        flatten_parameters = torch.cat(
            [param.view(-1) for param in pl_module.parameters()], dim=0
        )

        bottom_k, _ = torch.topk(
            flatten_parameters.abs(),
            int(self.sparsity_level * flatten_parameters.numel()),
            largest=False,
            sorted=True,
        )
        threshold = bottom_k.data[-1]
        self._prune_model(threshold, pl_module)

    @staticmethod
    def _prune_model(threshold: float, pl_module: pl.LightningModule) -> None:
        """
        Method used to zero parameters which magnitude are below `threshold`.

        Args:
            threshold (float):
                value below which weights will be pruned
            pl_module (pl.LightningModule):
                Lightning module
        """
        try:
            # Only prune the student when performing distillation
            pl_module = pl_module.get_submodule("student")
        except AttributeError:
            pass

        with torch.no_grad():
            for param in pl_module.parameters():
                mask = torch.lt(torch.abs(param), threshold)
                param.data[mask] = 0.0


class LayerPruning(Callback):
    """
    Callback for PyTorch Lightning to prune layers from an encoder and/or decoder in a model.

    This modifies the student model's encoder and decoder to have the specified number of
    layers by removing certain layers.

    Attributes:
        num_layers (int): An integer specifying the number of layers the encoder should have after pruning.
        num_decoder_layers (int): An integer specifying the number of layers the decoder should have after pruning.
    """

    def __init__(self, num_layers: int, num_decoder_layers: int, *args, **kwargs):
        """
        Initializes the LayerPruning callback.

        Args:
            num_layers: The desired number of encoder layers remaining after pruning.
            num_decoder_layers: The desired number of decoder layers remaining after pruning.
        """

        super().__init__(*args, **kwargs)
        self.num_decoder_layers = num_decoder_layers
        self.num_layers = num_layers

    def _layers_to_remove(self, final_size: int) -> List[int]:
        """
        Determines the layers to remove to achieve the desired final size.

        Args:
            final_size (int): An integer representing the final size of the encoder or decoder
                        after pruning the layers.

        Returns:
            List[int]: A list of ints representing the layers to be removed from the encoder or decoder.
        """
        return [i for i in range(1, final_size * 2, 2)]

    def setup(self, trainer, pl_module, stage):
        """
        Sets up the student model's encoder and/or decoder layers according to the specified numbers,
        by pruning the necessary layers.

        Called when fit or test begins. Override to define the setup logic.

        Args:
            trainer: The PyTorch Lightning Trainer instance.
            pl_module: The LightningModule instance being optimized or tested.
            stage: A string specifying the stage ('fit' or 'test') to trigger different behavior.
        """
        if stage == 'fit':
            model = pl_module.student

            if model.config.num_decoder_layers != self.num_decoder_layers:
                for i in reversed(self._layers_to_remove(self.num_decoder_layers)):
                    del model.decoder.block[i]
                model.config.num_decoder_layers = self.num_decoder_layers

            if model.config.num_layers != self.num_layers:
                for i in reversed(self._layers_to_remove(self.num_layers)):
                    del model.encoder.block[i]
                model.config.num_layers = self.num_layers
