import datetime
import os
import sys
import tempfile
from abc import abstractmethod
from typing import Any, Dict, Union

import gym
import numpy as np
import torch

from research.datasets import ReplayBuffer
from research.datasets.replay_buffer import storage
from research.envs.base import EmptyEnv
from research.utils import runners

from .base import Algorithm


class OffPolicyAlgorithm(Algorithm):
    def __init__(
        self,
        *args,
        offline_steps: int = 0,  # Run fully offline by setting to -1
        random_steps: int = 1000,
        async_runner_ep_lag: int = 1,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.offline_steps = offline_steps
        self.random_steps = random_steps
        self.async_runner_ep_lag = async_runner_ep_lag

    def setup_datasets(self, env: gym.Env, total_steps: int):
        super().setup_datasets(env, total_steps)
        # Assign the correct update function based on what is passed in.
        if env is None or isinstance(env, EmptyEnv) or self.offline_steps < 0:
            self.env_step = self._empty_step
        elif isinstance(env, runners.AsyncEnv):
            self._episode_reward = 0
            self._episode_length = 0
            self._num_ep = 0
            self._env_steps = 0
            self._resetting = True
            env.reset_send()  # Ask the env to start resetting.
            self.env_step = self._async_env_step
        elif isinstance(env, runners.MPRunner):
            assert isinstance(self.dataset, ReplayBuffer), "must use replaybuffer for MP RUnner."
            assert self.dataset.distributed, "ReplayBuffer must be distributed for use with Fully MPRunner."
            # Launch the runner subprocess.
            self._eps_since_last_checkpoint = 0
            self._checkpoint_dir = tempfile.mkdtemp(prefix="checkpoints_")
            assert self.offline_steps <= 0, "MPRunner does not currently support offline to online."
            env.start(
                fn=_off_policy_collector_subprocess,
                checkpoint_path=self._checkpoint_dir,
                storage_path=self.dataset.storage_path,
                random_steps=self.random_steps,
                total_steps=total_steps,
            )
            self.env_step = self._runner_env_step
        elif isinstance(env, gym.Env):
            # Setup Env Metrics
            self._current_obs = env.reset()
            self._episode_reward = 0
            self._episode_length = 0
            self._num_ep = 0
            self._env_steps = 0
            # Note that currently the very first (s, a) pair is thrown away because
            # we don't add to the dataset here.
            # This was done for better compatibility for offline to online learning.
            self.dataset.add(obs=self._current_obs)  # add the first observation.
            self.env_step = self._env_step
        else:
            raise ValueError("Invalid env passed")

    def _empty_step(self, env: gym.Env, step: int, total_steps: int) -> Dict:
        return dict()

    def _env_step(self, env: gym.Env, step: int, total_steps: int) -> Dict:
        # Return if env is Empty or we we aren't at every env_freq steps
        if step <= self.offline_steps:
            # Purposefully set to nan so we write CSV log.
            return dict(steps=self._env_steps, reward=-np.inf, length=np.inf, num_ep=self._num_ep)

        if step < self.random_steps:
            action = env.action_space.sample()
        else:
            self.eval()
            action = self._get_train_action(self._current_obs, step, total_steps)
            self.train()
        if isinstance(env.action_space, gym.spaces.Box):
            action = np.clip(action, env.action_space.low, env.action_space.high)

        next_obs, reward, done, info = env.step(action)
        self._env_steps += 1
        self._episode_length += 1
        self._episode_reward += reward

        if "discount" in info:
            discount = info["discount"]
        elif hasattr(env, "_max_episode_steps") and self._episode_length == env._max_episode_steps:
            discount = 1.0
        else:
            discount = 1 - float(done)

        # Store the consequences.
        self.dataset.add(obs=next_obs, action=action, reward=reward, done=done, discount=discount)

        if done:
            self._num_ep += 1
            # Compute metrics
            metrics = dict(
                steps=self._env_steps, reward=self._episode_reward, length=self._episode_length, num_ep=self._num_ep
            )
            # Reset the environment
            self._current_obs = env.reset()
            self.dataset.add(obs=self._current_obs)  # Add the first timestep
            self._episode_length = 0
            self._episode_reward = 0
            return metrics
        else:
            self._current_obs = next_obs
            return dict(steps=self._env_steps)

    def _async_env_step(self, env: gym.Env, step: int, total_steps: int) -> Dict:
        # RECIEVE DATA FROM THE LAST STEP
        if self._resetting:
            self._current_obs = env.reset_recv()
            self.dataset.add(obs=self._current_obs)
            self._resetting = False
            done = False
        else:
            self._current_obs, reward, done, info = env.step_recv()
            self._env_steps += 1
            self._episode_length += 1
            self._episode_reward += reward
            self.dataset.add(obs=self._current_obs, action=self._current_action, reward=reward, done=done, discount=info["discount"])

        # SEND DATA FOR THE NEXT STEP.
        if done:
            # If the episode terminated, then we need to reset and send the reset message
            self._resetting = True
            self._num_ep += 1
            metrics = dict(
                steps=self._env_steps, reward=self._episode_reward, length=self._episode_length, num_ep=self._num_ep
            )
            # Reset the environment
            self._current_obs = env.reset()
            self.dataset.add(obs=self._current_obs)  # Add the first timestep
            self._episode_length = 0
            self._episode_reward = 0
            env.reset_send()
            return metrics
        else:
            # Otherwise, compute the action we shoudl take and send it.
            if step < self.random_steps:
                self._current_action = env.action_space.sample()
            else:
                self.eval()
                self._current_action = self._get_train_action(self._current_obs, step, total_steps)
                self.train()
            if isinstance(env.action_space, gym.spaces.Box):
                self._current_action = np.clip(self._current_action, env.action_space.low, env.action_space.high)
            env.step_send(self._current_action)
            return dict(steps=self._env_steps)

    def _runner_env_step(self, env: gym.Env, step: int, total_steps: int) -> Dict:
        # All we do is check the pipe to see if there is data!
        metrics = env()
        if len(metrics) > 0:
            # If the metrics are non-empty, then it means that we have completed an episode.
            # As such, decrement the counter
            self._eps_since_last_checkpoint += 1
        if self._eps_since_last_checkpoint == self.async_runner_ep_lag:
            self.save(self._checkpoint_dir, str(step), dict(step=step))
            self._eps_since_last_checkpoint = 0
        return metrics

    @abstractmethod
    def _get_train_action(self, obs: Any, step: int, total_steps: int) -> np.ndarray:
        raise NotImplementedError


def _off_policy_collector_subprocess(
    env_fn,
    queue,
    config_path: str,
    checkpoint_path: str,
    storage_path: str,
    device: Union[str, torch.device] = "auto",
    random_steps: int = 0,
    total_steps: int = 0,
):
    """
    This subprocess loads a train environemnt.
    It then collects episodes with a loaded policy and saves them to disk.
    Afterwards, we check to see if there is an updated policy that we can use.
    """
    try:
        env = env_fn()
        # Load the model
        from research.utils.config import Config

        config = Config.load(config_path)
        config = config.parse()
        model = config.get_model(observation_space=env.observation_space, action_space=env.action_space, device=device)
        model.eval()

        # Metrics:
        num_ep = 0
        env_steps = 0
        current_checkpoint = None

        # Get the evaluation function.
        while True:
            # First, look for a checkpoint.
            checkpoints = os.listdir(checkpoint_path)
            if len(checkpoints) > 0:
                # Sort the the checkpoints by path
                checkpoints = sorted(checkpoints, key=lambda x: int(x[:-3]))
                checkpoints = [os.path.join(checkpoint_path, checkpoint) for checkpoint in checkpoints]
                if checkpoints[-1] != current_checkpoint and os.path.getsize(checkpoints[-1]) > 0:
                    try:
                        _ = model.load(checkpoints[-1])  # load the most recent one
                        # Remove all checkpoints that are not equal to the current one.
                        current_checkpoint = checkpoints[-1]
                        for checkpoint in checkpoints[:-1]:  # Ignore the last checkpoint, we loaded it.
                            os.remove(checkpoint)
                    except (EOFError, RuntimeError):
                        _ = model.load(current_checkpoint)

            # Then, collect an episode
            obs = env.reset()
            obses = [obs]
            actions = [env.action_space.sample()]
            rewards = [0.0]
            dones = [False]
            discounts = [1.0]
            done = False

            while not done:
                if env_steps < random_steps:
                    action = env.action_space.sample()
                else:
                    with torch.no_grad():
                        action = model._get_train_action(obs, env_steps, total_steps)

                obs, reward, done, info = env.step(action)
                env_steps += 1
                obses.append(obs)
                actions.append(action)
                rewards.append(reward)
                dones.append(done)
                if "discount" in info:
                    discount = info["discount"]
                elif hasattr(env, "_max_episode_steps") and len(dones) - 1 == env._max_episode_steps:
                    discount = 1.0
                else:
                    discount = 1 - float(done)
                discounts.append(discount)

            # The episode has terminated.
            num_ep += 1
            metrics = dict(steps=env_steps, reward=np.sum(reward), length=len(dones) - 1, num_ep=num_ep)
            queue.put(metrics)
            data = dict(obs=obses, action=actions, reward=rewards, done=dones, discount=discounts)
            # Timestamp it and add the ep idx (num ep - 1 so we start at zero.)
            ts = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")
            ep_filename = f"{ts}_{num_ep - 1}_{len(dones)}.npz"
            storage.save_data(data, os.path.join(storage_path, ep_filename))

    except KeyboardInterrupt:
        print("[research] OffPolicy Collector sent interrupt.")
        queue.put(None)  # Add None in the queue, ie failure!
    except Exception as e:
        print("[research] OffPolicy Collector Subprocess encountered exception.")
        print(e)
        print(sys.exec_info()[:2])
        queue.put(None)  # Add None in the queue, ie failure!
    finally:
        env.close()  # Close the env to prevent hanging threads.
