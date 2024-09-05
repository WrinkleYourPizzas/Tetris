import torch
import torch.nn as nn
from collections import deque
import numpy as np
import random
from alt_dqn import AltDeepQNetwork
from cnn import GABA
from envs import alt_tetris
from torch.utils.tensorboard import SummaryWriter

torch.backends.cuda.matmul.allow_tf32 = True
torch.backends.cudnn.allow_tf32 = True

device = torch.device("cuda") if torch.cuda.is_available() else torch.device("cpu")
writer = SummaryWriter()

GAMMA = 0.99
BATCH_SIZE = 512
REPLAY_SIZE = 30_000
MIN_REPLAY_SIZE = 1000
EPSILON_START = 1.00
EPSILON_END = 1e-3
EPSILON_DECAY = 2000
EPSILON_DECAY_RATE = 0.998
LEARNING_RATE = 1e-3
LEARNING_RATE_DECAY = 0.9
LEARNING_RATE_DECAY_FREQ = 500
NUM_ACTIONS = 1
NUM_EPOCHS = 3000
MAX_EPOCH_STEPS = 2000
TAU = 0.005
TARGET_UPDATE_FREQ = 500
SAVE_FREQ = 500

env = alt_tetris.TetrisEnv()

replay_memory = deque(maxlen=REPLAY_SIZE)
reward_memory = deque(maxlen=10)
reward_avg = 0
reward_avg_bench = 2500
episode_reward = 0.0
episode_losses = []

# policy_net = AltDeepQNetwork(NUM_ACTIONS, env).to(device)
# target_net = AltDeepQNetwork(NUM_ACTIONS, env).to(device)

policy_net = GABA(env).to(device)
target_net = GABA(env).to(device)

target_net.load_state_dict(policy_net.state_dict())

policy_net.train()
target_net.train()

optimizer = torch.optim.AdamW(policy_net.parameters(), lr=LEARNING_RATE, amsgrad=True)
criterion = nn.SmoothL1Loss()
scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(optimizer, patience=10)

example_state = torch.tensor(np.array([4, 5, 4, 3, 3, 2])).to(device)

state = env.reset()


def sigmoid(x):
    return 1 / (1 + np.exp(-1 * (x - 10)))


def get_lr(optimizer):
    for g in optimizer.param_groups:
        return g["lr"]


def calculate_priority():
    states = torch.as_tensor(
        np.array([m[0] for m in replay_memory]), dtype=torch.float32
    ).to(device)
    rewards = torch.as_tensor(
        np.array([m[2] for m in replay_memory]), dtype=torch.float32
    ).to(device)
    new_states = torch.as_tensor(
        np.array([m[4] for m in replay_memory]), dtype=torch.float32
    ).to(device)

    target_net.eval()
    policy_net.eval()
    with torch.no_grad():
        estimates = policy_net(states)
        targets = target_net(new_states)
    policy_net.train()
    target_net.train()

    priorities = np.array(np.abs(estimates - rewards - targets))
    probabilities = priorities / np.sum(priorities)
    replay_memory = np.array(replay_memory)
    replay_memory[:, 5] = probabilities
    replay_memory = deque(map(tuple, replay_memory))


# init replay memory
for _ in range(MIN_REPLAY_SIZE):
    r = np.random.randint(0, 4)
    x = np.random.randint(0, env.get_movement_bounds()[r])
    action = r * 10 + x

    new_state, reward, done, info = env.step(x, r, False)
    transition = (state, action, reward, done, new_state)
    replay_memory.append(transition)

    state = new_state

    if done == 1:
        state = env.reset()

# training
epoch = 0
epoch_step = 0
episode_lines_cleared = 0
epsilon = EPSILON_START
state = env.reset()

# training loop
while epoch < NUM_EPOCHS:
    rand_sample = random.random()
    next_states, actions = env.get_next_states()

    if rand_sample <= epsilon:
        index = np.random.randint(0, len(actions))
    else:
        input = torch.as_tensor(
            np.array([t for t in next_states]), dtype=torch.float32
        ).to(device)
        policy_net.eval()
        with torch.no_grad():
            q_values = policy_net(input)
        policy_net.train()
        index = torch.argmax(q_values, dim=0).item()

    next_state = next_states[index]
    action = actions[index]

    new_state, reward, done, lines_cleared = env.step(
        action % 10, int(action / 10), probe=False
    )
    transition = (state, action, reward, done, next_state, 0)
    replay_memory.append(transition)

    state = new_state
    episode_reward += reward
    episode_lines_cleared += lines_cleared

    if done == 1:
        epoch += 1
        state = env.reset()

        reward_memory.append(episode_reward)

    else:
        epoch_step += 1
        if epoch_step < MAX_EPOCH_STEPS:
            continue

    # sample from replay memory
    transitions = random.sample(replay_memory, BATCH_SIZE)

    states = torch.as_tensor(
        np.array([t[0] for t in transitions]), dtype=torch.float32
    ).to(device)
    actions = (
        torch.as_tensor(np.asarray([t[1] for t in transitions]), dtype=torch.int64)
        .unsqueeze(-1)
        .to(device)
    )
    rewards = (
        torch.as_tensor(np.asarray([t[2] for t in transitions]), dtype=torch.float32)
        .unsqueeze(-1)
        .to(device)
    )
    dones = (
        torch.as_tensor(np.asarray([t[3] for t in transitions]), dtype=torch.float32)
        .unsqueeze(-1)
        .to(device)
    )
    new_states = torch.as_tensor(
        np.array([t[4] for t in transitions]), dtype=torch.float32
    ).to(device)

    # calculate targets and q-values
    policy_net.eval()
    target_net.eval()
    q_values = policy_net(states)
    with torch.no_grad():
        target_q_values = target_net(new_states)
    policy_net.train()
    target_net.train()
    targets = rewards + (GAMMA * (1 - dones) * (target_q_values))

    # gradient descent
    loss = criterion(q_values, targets)
    optimizer.zero_grad()
    loss.backward()
    torch.nn.utils.clip_grad_value_(policy_net.parameters(), 100)
    optimizer.step()
    # scheduler.step(loss)

    # logs
    print()
    print("Epoch: ", epoch)
    print("Reward: ", episode_reward)
    print("Lines cleared: ", episode_lines_cleared)
    print("Epsilon: ", epsilon)
    print("LR: ", get_lr(optimizer))
    print("Loss: ", loss.item())

    writer.add_scalar("Reward", episode_reward, epoch)
    writer.add_scalar("Lines Cleared", episode_lines_cleared, epoch)
    writer.add_scalar("Epsilon", epsilon, epoch)
    writer.add_scalar("Learning Rate", get_lr(optimizer), epoch)
    writer.add_scalar("Loss", loss.item(), epoch)
    writer.flush()

    epoch_step = 0
    episode_reward = 0.0
    episode_lines_cleared = 0
    # epsilon = sigmoid(epoch)
    epsilon = np.interp(epoch, [0, EPSILON_DECAY], [EPSILON_START, EPSILON_END])
    # epsilon = EPSILON_END + (EPSILON_START - EPSILON_END) * math.exp(-1. * epoch / EPSILON_DECAY)

    # update taret network
    if epoch % TARGET_UPDATE_FREQ == 0:
        target_net.load_state_dict(policy_net.state_dict())

    # target_net_state_dict = target_net.state_dict()
    # policy_net_state_dict = policy_net.state_dict()
    # for key in policy_net_state_dict:
    #   target_net_state_dict[key] = policy_net_state_dict[key]*TAU + target_net_state_dict[key]*(1-TAU)
    # target_net.load_state_dict(target_net_state_dict)

    # save model at interval
    if epoch % SAVE_FREQ == 0 and epoch >= SAVE_FREQ:
        torch.save(policy_net.state_dict(), "policy_weights.pth")
        torch.save(target_net.state_dict(), "target_weights.pth")

    # save model passing benchmark
    avg = np.mean(np.array(reward_memory))
    if avg > reward_avg_bench and avg > reward_avg:
        torch.save(policy_net.state_dict(), "optimal_policy_weights.pth")
        torch.save(target_net.state_dict(), "optimal_target_weights.pth")
        reward_avg = avg

torch.save(policy_net.state_dict(), "policy_weights.pth")
torch.save(target_net.state_dict(), "target_weights.pth")
