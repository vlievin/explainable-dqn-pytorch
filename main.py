import copy
import os
from collections import namedtuple
from itertools import count
import math
import random
import numpy as np
import time
import logging
import argparse
from tqdm import tqdm
import torch.multiprocessing as mp

import gym
from gym import wrappers
import ptan

from wrappers import *
from memory import ReplayMemory
from models import *
from lunar_lander import LunarLander

import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F

Transition = namedtuple('Transion', ('state', 'action', 'next_state', 'reward'))


def select_action(policy, state, epsilon):
    """
    select action folowing an epsilon greedy policy
    :param state: current stae
    :return: action as a long tensor
    """
    policy.eval()

    # policy
    sample = random.random()
    if sample > epsilon:
        with torch.no_grad():
            return policy(state).max(1)[1].view(1, 1)
    else:
        return torch.tensor([[random.randrange(N_ACTIONS)]], device=device, dtype=torch.long)


def update_epsilon(steps_done):
    # epsilon decay
    if steps_done > EPS_OFFSET:
        epsilon = EPS_END + (EPS_START - EPS_END) * math.exp(-1. * (steps_done - EPS_OFFSET) / (EPS_DECAY + EPS_OFFSET))
    else:
        epsilon = EPS_START

    return epsilon

def sample_memory(memory, device, non_blocking=False):
    # sample replay buffer
    transitions = memory.sample(BATCH_SIZE)
    batch = Transition(*zip(*transitions))

    # convert to tensors and create batches
    actions = batch.action
    rewards = batch.reward

    non_final_mask = torch.tensor(tuple(map(lambda s: s is not None, batch.next_state)), device=device, dtype=torch.uint8)
    non_final_next_states = torch.cat([s for s in batch.next_state if s is not None]).to(device, non_blocking=non_blocking)
    state_batch = torch.cat(batch.state).to(device, non_blocking=non_blocking)
    action_batch = torch.cat(actions).to(device, non_blocking=non_blocking)
    reward_batch = torch.cat(rewards).to(device, non_blocking=non_blocking)

    return state_batch, action_batch, reward_batch, non_final_mask, non_final_next_states


def optimize_model(policy_net, target_net, memory, batch = None, use_double_dqn=False):
    """
    optimize policy
    :param use_double_dqn: use double DQN
    :return: None
    """

    policy_net.train()
    target_net.eval()

    if len(memory) < BATCH_SIZE:
        return

    if batch is None:
        batch = sample_memory(memory, device)

    state_batch, action_batch, reward_batch, non_final_mask, non_final_next_states = batch

    state_action_values = policy_net(state_batch).gather(1, action_batch)

    next_state_values = torch.zeros(BATCH_SIZE, device=device)

    # Compute expected state action value
    if use_double_dqn:
        argmax_next_state_values = policy_net(non_final_next_states).argmax(1).detach().unsqueeze(1)
        next_state_values[non_final_mask] = target_net(non_final_next_states).gather(1,
                                                                                     argmax_next_state_values).detach().squeeze()

        expected_state_action_values = (next_state_values * GAMMA) + reward_batch
    else:
        next_state_values[non_final_mask] = target_net(non_final_next_states).max(1)[0].detach()
        expected_state_action_values = (next_state_values * GAMMA) + reward_batch

    # compute loss
    loss = F.smooth_l1_loss(state_action_values, expected_state_action_values.unsqueeze(1))

    # optimize
    optimizer.zero_grad()
    loss.backward()
    #for param in policy_net.parameters():
    #    param.grad.data.clamp_(-1, 1)
    optimizer.step()


def get_state(obs):
    """
    create state tensor from observation
    :param obs: observation from the environment
    :return: state as torch tensor of shape [1 x *]
    """
    if RAM:
        state = torch.tensor(obs, dtype=torch.float)
    else:
        state = torch.tensor(obs.__array__())
    return state.unsqueeze(0).contiguous()


def train(env, policy_net, target_net, memory, n_episodes, render=False, double_dqn=False):
    """
    train the policy model for n_episodes
    :param env: gym environment
    :param n_episodes: number of episodes
    :param render: if True, renders the environment
    :param double_dqn: use double DQN policy
    :return: None
    """
    steps_done = 0

    for episode in tqdm(range(n_episodes)):
        obs = env.reset()
        state = get_state(obs).to(device, non_blocking=True)
        total_reward = 0.
        elaps = time.time()
        frames = 0
        total_opt_time = 0.
        batch = None
        for t in count():

            epsilon = update_epsilon(steps_done)
            steps_done += 1
            frames += 1

            action = select_action(policy_net, state, epsilon)

            if render:
                env.render()

            obs, reward, done, info = env.step(action)

            total_reward += reward

            if not done:
                next_state = get_state(obs).to(device, non_blocking=True)
            else:
                next_state = None

            reward = torch.tensor([reward], device=device, dtype=torch.float)

            memory.push(state, action, next_state, reward)

            state = next_state

            if steps_done > INITIAL_MEMORY:

                if t % PLAY_STEPS == 0:
                    opt_time = time.time()
                    optimize_model(policy_net, target_net, memory, batch=batch, use_double_dqn=double_dqn)
                    total_opt_time += time.time() - opt_time
                    batch = sample_memory(memory, device, non_blocking=True)

                if steps_done % TARGET_UPDATE == 0:
                    target_net.load_state_dict(policy_net.state_dict())

            if done:
                break

        if episode % LOG_FREQ == 0:
            elaps = (time.time() - elaps)
            frames_seconds = frames / elaps
            logger.info(
                f'Total steps: {steps_done}   Episode: {episode}/{t}   Epsilon {epsilon:.3f}   Fps {frames_seconds:.3f}   Time ({elaps:.3f} / {total_opt_time:.3f})   Total reward: {total_reward:.0f}')
    env.close()
    return


def test(env, n_episodes, policy, logdir, render=True):
    policy.eval()

    #env = gym.wrappers.Monitor(env, logdir, force=True)
    for episode in range(n_episodes):
        obs = env.reset()
        state = get_state(obs)
        total_reward = 0.0
        for t in count():
            action = policy(state.to(device)).max(1)[1].view(1, 1)

            if HUMAN:
                if not human_sets_pause:
                    action = human_agent_action
                else:
                    pass

            if render:
                env.render()
                time.sleep(0.1)

            obs, reward, done, info = env.step(action)

            total_reward += reward

            if not done:
                next_state = get_state(obs)
            else:
                next_state = None

            state = next_state

            if done:
                logger.info("Finished Episode {} with reward {}".format(episode, total_reward))
                break

    env.close()
    return


if __name__ == '__main__':
    """
    train and/or evaluate an agent. 
    You can play Atari yourself usingthe flags --render --human 
    (use arrow keys to control the agent and space bar to gives control to the agent) 
    """


    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=str, default='logs/', help='log directory')
    parser.add_argument('--version', default='8', help='version')
    parser.add_argument('--env', default="Pong", help='gym env')
    parser.add_argument('--env_version', default="NoFrameskip-v4", help='gym env')
    parser.add_argument('--episodes', type=int, default='1000', help='number of episodes')
    parser.add_argument('--log_freq', type=int, default='1', help='log frequency')
    parser.add_argument('--batch_size', type=int, default='32', help='batch size')
    parser.add_argument('--memory_size', type=int, default='100000', help='memory size')
    parser.add_argument('--initial_memory_size', type=int, default='10000', help='initial memory size')
    parser.add_argument('--epsilon_decay', type=int, default='100000', help='number of steps to decrease epsilon')
    parser.add_argument('--min_epsilon', type=float, default=0.02, help='minimum epsilon')
    parser.add_argument('--play_steps', type=int, default=4, help='number of playing steps without optimization')
    parser.add_argument('--target_update_freq', type=int, default='1000', help='number of episodes before each target update')
    parser.add_argument('--double', action='store_true', help='use double DQN')
    parser.add_argument('--dueling', action='store_true', help='use dueling DQN')
    parser.add_argument('--gamma', type=float, default=0.99, help='gamma parameter')
    parser.add_argument('--lr', type=float, default=1e-4, help='learning rate')
    parser.add_argument('--render', action='store_true', help='render environment')
    parser.add_argument('--evaluate', action='store_true', help='evalaute trained model')
    parser.add_argument('--human', action='store_true', help='play yourself')
    opt = parser.parse_args()

    env_id = f"{opt.env}{opt.env_version}"

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(env_id, "\nDEVICE:", device, "\n")

    if not os.path.exists(opt.root):
        os.makedirs(opt.root)

    # define unique id
    model_id = 'DDQN' if opt.dueling else 'DQN'
    if opt.double:
        model_id = 'Double' + model_id
    run_id = f"{model_id}-{env_id}-version-{opt.version}-{opt.batch_size}-{opt.gamma}-{opt.lr}-{opt.play_steps}-{opt.memory_size}"

    # logging
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(name)-4s %(levelname)-4s %(message)s',
                        datefmt='%m-%d %H:%M',
                        handlers=[logging.FileHandler(f"{opt.root}/{run_id}.log"),
                                  logging.StreamHandler()])
    logger = logging.getLogger(run_id)

    # parameters
    seed = 42
    torch.manual_seed(seed)
    np.random.seed(seed)
    BATCH_SIZE = opt.batch_size * opt.play_steps
    GAMMA = opt.gamma
    EPS_START = 1
    EPS_END = opt.min_epsilon
    EPS_DECAY = opt.epsilon_decay
    EPS_OFFSET = opt.initial_memory_size
    TARGET_UPDATE = opt.target_update_freq
    LOG_FREQ = opt.log_freq
    RENDER = opt.render
    lr = opt.lr
    INITIAL_MEMORY = opt.initial_memory_size
    MEMORY_SIZE = opt.memory_size
    PLAY_STEPS = opt.play_steps
    HUMAN = opt.human

    # create environment
    if 'lunar' in opt.env:
        env = LunarLander()
        RAM = True
    else:
        env = gym.make(env_id)
        #env = make_env(env)
        env = ptan.common.wrappers.wrap_dqn(env)
        RAM = False

    N_ACTIONS = env.action_space.n

    # human mode
    if HUMAN:
        RENDER = True
        from pyglet.window import key as KEY

        SKIP_CONTROL = 0  # Use previous control decision SKIP_CONTROL times, that's how you
        # can test what skip is still usable.

        human_agent_action = 0
        human_wants_restart = False
        human_sets_pause = False


        def key_press(key, mod):
            global human_agent_action, human_wants_restart, human_sets_pause

            if key == 0xff0d: human_wants_restart = True
            if key == 32: human_sets_pause = not human_sets_pause

            if key == KEY.LEFT:  human_agent_action = 0
            if key == KEY.RIGHT: human_agent_action = 2
            if key == KEY.UP:    human_agent_action = 4
            if key == KEY.DOWN:  human_agent_action = 3


        def key_release(key, mod):
            global human_agent_action
            human_agent_action = 1


        env.render()
        env.unwrapped.viewer.window.on_key_press = key_press
        env.unwrapped.viewer.window.on_key_release = key_release

    # create models
    if RAM:
        a = random.randrange(env.action_space.n)
        s, r, done, info = env.step(a)
        N_STATE = len(s)
        MODEL = LanderDQN if 'lander' in opt.env else RamDQN
        policy_net = MODEL(N_STATE, N_ACTIONS).to(device)
        target_net = MODEL(N_STATE, N_ACTIONS).to(device)
    else:
        MODEL = DDQNbn if opt.dueling else DQNbn
        policy_net = MODEL(n_actions=N_ACTIONS).to(device)
        target_net = MODEL(n_actions=N_ACTIONS).to(device)

    target_net.load_state_dict(policy_net.state_dict())

    # setup optimizer
    optimizer = optim.Adam(policy_net.parameters(), lr=lr)

    # initialize replay memory
    memory = ReplayMemory(MEMORY_SIZE)

    # train model and evaluate
    if not opt.evaluate:
        train(env, policy_net, target_net, memory, opt.episodes, double_dqn=opt.double, render=RENDER)
        torch.save(policy_net, f"{opt.root}/{run_id}.pt")
    policy_net = torch.load(f"{opt.root}/{run_id}.pt", map_location=device)
    logdir = f"{opt.root}/.videos/{run_id}/"
    test(env, 10, policy_net, logdir, render=RENDER)