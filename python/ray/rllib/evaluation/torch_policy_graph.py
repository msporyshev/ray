from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os

import numpy as np
from threading import Lock

try:
    import torch
except ImportError:
    pass  # soft dep

from ray.rllib.evaluation.metrics import LEARNER_STATS_KEY
from ray.rllib.evaluation.policy_graph import PolicyGraph
from ray.rllib.utils.annotations import override
from ray.rllib.utils.tracking_dict import UsageTrackingDict


class TorchPolicyGraph(PolicyGraph):
    """Template for a PyTorch policy and loss to use with RLlib.

    This is similar to TFPolicyGraph, but for PyTorch.

    Attributes:
        observation_space (gym.Space): observation space of the policy.
        action_space (gym.Space): action space of the policy.
        lock (Lock): Lock that must be held around PyTorch ops on this graph.
            This is necessary when using the async sampler.
    """

    def __init__(self, observation_space, action_space, model, loss,
                 action_distribution_cls):
        """Build a policy graph from policy and loss torch modules.

        Note that model will be placed on GPU device if CUDA_VISIBLE_DEVICES
        is set. Only single GPU is supported for now.

        Arguments:
            observation_space (gym.Space): observation space of the policy.
            action_space (gym.Space): action space of the policy.
            model (nn.Module): PyTorch policy module. Given observations as
                input, this module must return a list of outputs where the
                first item is action logits, and the rest can be any value.
            loss (func): Function that takes (policy_graph, batch_tensors)
                and returns a single scalar loss.
            action_distribution_cls (ActionDistribution): Class for action
                distribution.
        """
        self.observation_space = observation_space
        self.action_space = action_space
        self.lock = Lock()
        self.device = (torch.device("cuda")
                       if bool(os.environ.get("CUDA_VISIBLE_DEVICES", None))
                       else torch.device("cpu"))
        self._model = model.to(self.device)
        self._loss = loss
        self._optimizer = self.optimizer()
        self._action_dist_cls = action_distribution_cls

    @override(PolicyGraph)
    def compute_actions(self,
                        obs_batch,
                        state_batches=None,
                        prev_action_batch=None,
                        prev_reward_batch=None,
                        info_batch=None,
                        episodes=None,
                        **kwargs):
        with self.lock:
            with torch.no_grad():
                ob = torch.from_numpy(np.array(obs_batch)) \
                    .float().to(self.device)
                model_out = self._model({"obs": ob}, state_batches)
                logits, _, vf, state = model_out
                action_dist = self._action_dist_cls(logits)
                actions = action_dist.sample()
                return (actions.cpu().numpy(),
                        [h.cpu().numpy() for h in state],
                        self.extra_action_out(model_out))

    @override(PolicyGraph)
    def learn_on_batch(self, postprocessed_batch):
        batch_tensors = self._lazy_tensor_dict(postprocessed_batch)

        with self.lock:
            loss_out = self._loss(self, batch_tensors)
            self._optimizer.zero_grad()
            loss_out.backward()

            grad_process_info = self.extra_grad_process()
            self._optimizer.step()

            grad_info = self.extra_grad_info(batch_tensors)
            grad_info.update(grad_process_info)
            return {LEARNER_STATS_KEY: grad_info}

    @override(PolicyGraph)
    def compute_gradients(self, postprocessed_batch):
        batch_tensors = self._lazy_tensor_dict(postprocessed_batch)

        with self.lock:
            loss_out = self._loss(self, batch_tensors)
            self._optimizer.zero_grad()
            loss_out.backward()

            grad_process_info = self.extra_grad_process()

            # Note that return values are just references;
            # calling zero_grad will modify the values
            grads = []
            for p in self._model.parameters():
                if p.grad is not None:
                    grads.append(p.grad.data.cpu().numpy())
                else:
                    grads.append(None)

            grad_info = self.extra_grad_info(batch_tensors)
            grad_info.update(grad_process_info)
            return grads, {LEARNER_STATS_KEY: grad_info}

    @override(PolicyGraph)
    def apply_gradients(self, gradients):
        with self.lock:
            for g, p in zip(gradients, self._model.parameters()):
                if g is not None:
                    p.grad = torch.from_numpy(g).to(self.device)
            self._optimizer.step()

    @override(PolicyGraph)
    def get_weights(self):
        with self.lock:
            return {k: v.cpu() for k, v in self._model.state_dict().items()}

    @override(PolicyGraph)
    def set_weights(self, weights):
        with self.lock:
            self._model.load_state_dict(weights)

    @override(PolicyGraph)
    def get_initial_state(self):
        return [s.numpy() for s in self._model.state_init()]

    def extra_grad_process(self):
        """Allow subclass to do extra processing on gradients and
           return processing info."""
        return {}

    def extra_action_out(self, model_out):
        """Returns dict of extra info to include in experience batch.

        Arguments:
            model_out (list): Outputs of the policy model module."""
        return {}

    def extra_grad_info(self, batch_tensors):
        """Return dict of extra grad info."""

        return {}

    def optimizer(self):
        """Custom PyTorch optimizer to use."""
        if hasattr(self, "config"):
            return torch.optim.Adam(
                self._model.parameters(), lr=self.config["lr"])
        else:
            return torch.optim.Adam(self._model.parameters())

    def _lazy_tensor_dict(self, postprocessed_batch):
        batch_tensors = UsageTrackingDict(postprocessed_batch)
        batch_tensors.set_get_interceptor(
            lambda arr: torch.from_numpy(arr).to(self.device))
        return batch_tensors
