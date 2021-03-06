import torch

from catalyst.rl import utils
from .critic import OffpolicyCritic


class DQN(OffpolicyCritic):
    """
    Swiss Army knife DQN algorithm.
    """

    def _init(self, entropy_regularization: float = None):
        self.entropy_regularization = entropy_regularization

        # value distribution approximation
        critic_distribution = self.critic.distribution
        self._loss_fn = self._base_loss
        self._num_heads = self.critic.num_heads
        self._hyperbolic_constant = self.critic.hyperbolic_constant
        self._gammas = \
            utils.hyperbolic_gammas(
                self._gamma,
                self._hyperbolic_constant,
                self._num_heads
            )
        self._gammas = utils.any2device(self._gammas, device=self._device)
        assert critic_distribution in [None, "categorical", "quantile"]

        if critic_distribution == "categorical":
            assert self.critic_criterion is None
            self.num_atoms = self.critic.num_atoms
            values_range = self.critic.values_range
            self.v_min, self.v_max = values_range
            self.delta_z = (self.v_max - self.v_min) / (self.num_atoms - 1)
            z = torch.linspace(
                start=self.v_min, end=self.v_max, steps=self.num_atoms
            )
            self.z = utils.any2device(z, device=self._device)
            self._loss_fn = self._categorical_loss
        elif critic_distribution == "quantile":
            assert self.critic_criterion is not None
            self.num_atoms = self.critic.num_atoms
            tau_min = 1 / (2 * self.num_atoms)
            tau_max = 1 - tau_min
            tau = torch.linspace(
                start=tau_min, end=tau_max, steps=self.num_atoms
            )
            self.tau = utils.any2device(tau, device=self._device)
            self._loss_fn = self._quantile_loss
        else:
            assert self.critic_criterion is not None

    def _process_components(self, done_t, rewards_t):
        # Array of size [num_heads,]
        gammas = self._gammas ** self._n_step
        gammas = gammas[None, :, None]  # [1; num_heads; 1]
        # We use the same done_t, rewards_t, actions_t for each head
        done_t = done_t[:, None, :]  # [bs; 1; 1]
        rewards_t = rewards_t[:, None, :]  # [bs; 1; 1]

        return gammas, done_t, rewards_t

    def _compute_entropy(self, q_values_t):
        logprobs = torch.log_softmax(q_values_t, dim=-1)
        entropy = -(torch.exp(logprobs) * logprobs).mean()
        return entropy

    def _base_loss(self, states_t, actions_t, rewards_t, states_tp1, done_t):
        gammas, done_t, rewards_t = self._process_components(done_t, rewards_t)

        # [bs; 1] ->
        # [bs; num_heads; 1]
        actions_t = actions_t.unsqueeze(1).repeat(1, self._num_heads, 1)

        # [bs; num_heads; num_actions, 1] ->
        # [bs; num_heads; num_actions]
        q_values_t = self.critic(states_t).squeeze(-1)

        # [bs; num_heads; num_actions] -> gathering selected actions
        # [bs; num_heads; 1] -> many-heads view transform
        # [{bs * num_heads}; 1]
        action_q_values_t = q_values_t.gather(-1, actions_t).view(-1, 1)

        # [bs; num_heads; num_actions, 1] ->
        # [bs; num_heads; num_actions] -> max
        # [bs; num_heads; 1]
        q_values_tp1 = \
            self.target_critic(states_tp1).squeeze(-1).max(-1, keepdim=True)[0]

        # [bs; num_heads; 1] -> many-heads view transform
        # [{bs * num_heads}; 1]
        q_target_t = (
            rewards_t + (1 - done_t) * gammas * q_values_tp1
        ).view(-1, 1).detach()

        value_loss = \
            self.critic_criterion(action_q_values_t, q_target_t).mean()

        if self.entropy_regularization is not None:
            value_loss -= \
                self.entropy_regularization * self._compute_entropy(q_values_t)

        return value_loss

    def _categorical_loss(
        self, states_t, actions_t, rewards_t, states_tp1, done_t
    ):
        gammas, done_t, rewards_t = self._process_components(done_t, rewards_t)

        # [bs; 1] ->
        # [bs; 1; 1; 1;]
        actions_t = actions_t[:, None, None, :]

        # [bs; num_heads; 1; num_atoms]
        indices_t = actions_t.repeat(1, self._num_heads, 1, self.num_atoms)
        # [bs; num_heads; num_actions; num_atoms]
        q_logits_t = self.critic(states_t)
        # [bs; num_heads; 1; num_atoms] -> gathering selected actions
        # [bs; num_heads; num_atoms] -> many-heads view transform
        # [{bs * num_heads}; num_atoms]
        logits_t = (
            q_logits_t.gather(-2, indices_t).squeeze(-2)
            .view(-1, self.num_atoms)
        )

        # [bs; num_heads; num_actions; num_atoms]
        q_logits_tp1 = self.target_critic(states_tp1).detach()

        # [bs; num_heads; num_actions; num_atoms] -> categorical value
        # [bs; num_heads; num_actions] -> gathering best actions
        # [bs; num_heads; 1]
        actions_tp1 = (
            (torch.softmax(q_logits_tp1, dim=-1) * self.z)
            .sum(dim=-1)
            .argmax(dim=-1, keepdim=True)
        )
        # [bs; num_heads; 1] ->
        # [bs; num_heads; 1; 1] ->
        # [bs; num_heads; 1; num_atoms]
        indices_tp1 = actions_tp1.unsqueeze(-1).repeat(1, 1, 1, self.num_atoms)
        # [bs; num_heads; 1; num_atoms] -> gathering best actions
        # [bs; num_heads; num_atoms] -> many-heads view transform
        # [{bs * num_heads}; num_atoms]
        logits_tp1 = (
            q_logits_tp1.gather(-2, indices_tp1).squeeze(-2)
            .view(-1, self.num_atoms)
        ).detach()

        # [bs; num_heads; num_atoms] -> many-heads view transform
        # [{bs * num_heads}; num_atoms]
        atoms_target_t = (
            rewards_t + (1 - done_t) * gammas * self.z
        ).view(-1, self.num_atoms).detach()

        value_loss = utils.categorical_loss(
            # [{bs * num_heads}; num_atoms]
            logits_t,
            # [{bs * num_heads}; num_atoms]
            logits_tp1,
            # [{bs * num_heads}; num_atoms]
            atoms_target_t,
            self.z, self.delta_z,
            self.v_min, self.v_max
        )

        if self.entropy_regularization is not None:
            q_values_t = torch.sum(
                torch.softmax(q_logits_t, dim=-1) * self.z, dim=-1
            )
            value_loss -= \
                self.entropy_regularization * self._compute_entropy(q_values_t)

        return value_loss

    def _quantile_loss(
        self, states_t, actions_t, rewards_t, states_tp1, done_t
    ):
        gammas, done_t, rewards_t = self._process_components(done_t, rewards_t)

        # [bs; 1] ->
        # [bs; 1; 1; 1;]
        actions_t = actions_t[:, None, None, :]

        # [bs; num_heads; 1; num_atoms]
        indices_t = actions_t.repeat(1, self._num_heads, 1, self.num_atoms)
        # [bs; num_heads; num_actions; num_atoms]
        q_atoms_t = self.critic(states_t)
        # [bs; num_heads; 1; num_atoms] -> gathering selected actions
        # [bs; num_heads; num_atoms] -> many-heads view transform
        # [{bs * num_heads}; num_atoms]
        atoms_t = (
            q_atoms_t.gather(-2, indices_t).squeeze(-2)
            .view(-1, self.num_atoms)
        )

        # [bs; num_heads; num_actions; num_atoms]
        q_atoms_tp1 = self.target_critic(states_tp1)

        # [bs; num_heads; num_actions; num_atoms] -> quantile value
        # [bs; num_heads; num_actions] -> gathering best actions
        # [bs; num_heads; 1]
        actions_tp1 = (
            q_atoms_tp1
            .mean(dim=-1)
            .argmax(dim=-1, keepdim=True)
        )
        # [bs; num_heads; 1] ->
        # [bs; num_heads; 1; 1] ->
        # [bs; num_heads; 1; num_atoms]
        indices_tp1 = actions_tp1.unsqueeze(-1).repeat(1, 1, 1, self.num_atoms)
        # [bs; num_heads; 1; num_atoms] -> gathering best actions
        # [bs; num_heads; num_atoms]
        atoms_tp1 = q_atoms_tp1.gather(-2, indices_tp1).squeeze(-2)

        # [bs; num_heads; num_atoms] -> many-heads view transform
        # [{bs * num_heads}; num_atoms]
        atoms_target_t = (
            rewards_t + (1 - done_t) * gammas * atoms_tp1
        ).view(-1, self.num_atoms).detach()

        value_loss = utils.quantile_loss(
            # [{bs * num_heads}; num_atoms]
            atoms_t,
            # [{bs * num_heads}; num_atoms]
            atoms_target_t,
            self.tau, self.num_atoms,
            self.critic_criterion
        )

        if self.entropy_regularization is not None:
            q_values_t = torch.mean(q_atoms_t, dim=-1)
            value_loss -= \
                self.entropy_regularization * self._compute_entropy(q_values_t)

        return value_loss

    def update_step(self, value_loss, critic_update=True):
        # critic update
        critic_update_metrics = {}
        if critic_update:
            critic_update_metrics = self.critic_update(value_loss) or {}

        loss = value_loss
        metrics = {"loss": loss.item(), "loss_critic": value_loss.item()}
        metrics = {**metrics, **critic_update_metrics}

        return metrics
