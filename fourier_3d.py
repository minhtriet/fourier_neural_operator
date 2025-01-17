"""
@author: Zongyi Li
This file is the Fourier Neural Operator for 3D problem such as the Navier-Stokes equation discussed in Section 5.3 in the [paper](https://arxiv.org/pdf/2010.08895.pdf),
which takes the 2D spatial + 1D temporal equation directly as a 3D problem
"""
import torch
# best: 8.4, 367 iter   6.353 1k iter ( weight_decay=1e-1)  6.13
import torch.nn.functional as F
from utilities3 import *
from timeit import default_timer
from sklearn import decomposition

torch.manual_seed(0)
np.random.seed(0)

################################################################
# 3d fourier layers
################################################################

class SpectralConv3d(nn.Module):
    def __init__(self, in_channels, out_channels, modes1, modes2, modes3):
        super(SpectralConv3d, self).__init__()

        """
        3D Fourier layer. It does FFT, linear transform, and Inverse FFT.    
        """

        self.in_channels = in_channels
        self.out_channels = out_channels
        self.modes1 = modes1 #Number of Fourier modes to multiply, at most floor(N/2) + 1
        self.modes2 = modes2
        self.modes3 = modes3

        self.scale = (1 / (in_channels * out_channels))
        self.weights1 = nn.Parameter(self.scale * torch.rand(in_channels, out_channels, self.modes1, self.modes2, self.modes3, dtype=torch.cfloat))
        self.weights2 = nn.Parameter(self.scale * torch.rand(in_channels, out_channels, self.modes1, self.modes2, self.modes3, dtype=torch.cfloat))
        self.weights3 = nn.Parameter(self.scale * torch.rand(in_channels, out_channels, self.modes1, self.modes2, self.modes3, dtype=torch.cfloat))
        self.weights4 = nn.Parameter(self.scale * torch.rand(in_channels, out_channels, self.modes1, self.modes2, self.modes3, dtype=torch.cfloat))

    # Complex multiplication
    def compl_mul3d(self, input, weights):
        # (batch, in_channel, x,y,t ), (in_channel, out_channel, x,y,t) -> (batch, out_channel, x,y,t)
        return torch.einsum("bixyz,ioxyz->boxyz", input, weights)

    def forward(self, x):
        batchsize = x.shape[0]
        #Compute Fourier coeffcients up to factor of e^(- something constant)
        x_ft = torch.fft.rfftn(x, dim=[-3,-2,-1])

        # Multiply relevant Fourier modes
        out_ft = torch.zeros(batchsize, self.out_channels, x.size(-3), x.size(-2), x.size(-1)//2 + 1, dtype=torch.cfloat, device=x.device)
        out_ft[:, :, :self.modes1, :self.modes2, :self.modes3] = \
            self.compl_mul3d(x_ft[:, :, :self.modes1, :self.modes2, :self.modes3], self.weights1)
        out_ft[:, :, -self.modes1:, :self.modes2, :self.modes3] = \
            self.compl_mul3d(x_ft[:, :, -self.modes1:, :self.modes2, :self.modes3], self.weights2)
        out_ft[:, :, :self.modes1, -self.modes2:, :self.modes3] = \
            self.compl_mul3d(x_ft[:, :, :self.modes1, -self.modes2:, :self.modes3], self.weights3)
        out_ft[:, :, -self.modes1:, -self.modes2:, :self.modes3] = \
            self.compl_mul3d(x_ft[:, :, -self.modes1:, -self.modes2:, :self.modes3], self.weights4)

        #Return to physical space
        x = torch.fft.irfftn(out_ft, s=(x.size(-3), x.size(-2), x.size(-1)))
        return x

class MLP(nn.Module):
    def __init__(self, in_channels, out_channels, mid_channels):
        super(MLP, self).__init__()
        self.mlp1 = nn.Conv3d(in_channels, mid_channels, 1)
        self.mlp2 = nn.Conv3d(mid_channels, out_channels, 1)

    def forward(self, x):
        x = self.mlp1(x)
        x = F.gelu(x)
        x = self.mlp2(x)
        return x

class FNO3d(nn.Module):
    def __init__(self, modes1, modes2, modes3, width):
        super(FNO3d, self).__init__()

        """
        The overall network. It contains 4 layers of the Fourier layer.
        1. Lift the input to the desire channel dimension by self.fc0 .
        2. 4 layers of the integral operators u' = (W + K)(u).
            W defined by self.w; K defined by self.conv .
        3. Project from the channel space to the output space by self.fc1 and self.fc2 .
        
        input: the solution of the first 10 timesteps + 3 locations (u(1, x, y), ..., u(10, x, y),  x, y, t). It's a constant function in time, except for the last index.
        input shape: (batchsize, x=64, y=64, t=40, c=13)
        output: the solution of the next 40 timesteps
        output shape: (batchsize, x=64, y=64, t=40, c=1)
        """

        self.modes1 = modes1
        self.modes2 = modes2
        self.modes3 = modes3
        self.width = width
        self.padding = 6 # pad the domain if input is non-periodic

        self.p = nn.Linear(T+3, self.width)# input channel is 12: the solution of the first 10 timesteps + 3 locations (u(1, x, y), ..., u(10, x, y),  x, y, t)
        self.conv0 = SpectralConv3d(self.width, self.width, self.modes1, self.modes2, self.modes3)
        self.conv1 = SpectralConv3d(self.width, self.width, self.modes1, self.modes2, self.modes3)
        self.conv2 = SpectralConv3d(self.width, self.width, self.modes1, self.modes2, self.modes3)
        self.conv3 = SpectralConv3d(self.width, self.width, self.modes1, self.modes2, self.modes3)
        self.mlp0 = MLP(self.width, self.width, self.width)
        self.mlp1 = MLP(self.width, self.width, self.width)
        self.mlp2 = MLP(self.width, self.width, self.width)
        self.mlp3 = MLP(self.width, self.width, self.width)
        self.w0 = nn.Conv3d(self.width, self.width, 1)
        self.w1 = nn.Conv3d(self.width, self.width, 1)
        self.w2 = nn.Conv3d(self.width, self.width, 1)
        self.w3 = nn.Conv3d(self.width, self.width, 1)
        self.q = MLP(self.width, 1, self.width * 4) # output channel is 1: u(x, y)

    def forward(self, x):
        grid = self.get_grid(x.shape, x.device)
        x = torch.cat((x, grid), dim=-1)
        x = self.p(x)
        x = x.permute(0, 4, 1, 2, 3)
        x = F.pad(x, [0,self.padding]) # pad the domain if input is non-periodic

        x1 = self.conv0(x)
        x1 = self.mlp0(x1)
        x2 = self.w0(x)
        x = x1 + x2
        x = F.gelu(x)

        x1 = self.conv1(x)
        x1 = self.mlp1(x1)
        x2 = self.w1(x)
        x = x1 + x2
        x = F.gelu(x)

        x1 = self.conv2(x)
        x1 = self.mlp2(x1)
        x2 = self.w2(x)
        x = x1 + x2
        x = F.gelu(x)

        x1 = self.conv3(x)
        x1 = self.mlp3(x1)
        x2 = self.w3(x)
        x = x1 + x2

        x = x[..., :-self.padding]
        x = self.q(x)
        x = x.permute(0, 2, 3, 4, 1) # pad the domain if input is non-periodic
        return x


    def get_grid(self, shape, device):
        batchsize, size_x, size_y, size_z = shape[0], shape[1], shape[2], shape[3]
        gridx = torch.tensor(np.linspace(0, 1, size_x), dtype=torch.float)
        gridx = gridx.reshape(1, size_x, 1, 1, 1).repeat([batchsize, 1, size_y, size_z, 1])
        gridy = torch.tensor(np.linspace(0, 1, size_y), dtype=torch.float)
        gridy = gridy.reshape(1, 1, size_y, 1, 1).repeat([batchsize, size_x, 1, size_z, 1])
        gridz = torch.tensor(np.linspace(0, 1, size_z), dtype=torch.float)
        gridz = gridz.reshape(1, 1, 1, size_z, 1).repeat([batchsize, size_x, size_y, 1, 1])
        return torch.cat((gridx, gridy, gridz), dim=-1).to(device)

################################################################
# configs
################################################################

TRAIN_PATH = 'data/train_p.pkl'
VAL_PATH = 'data/val_p.pkl'
TEST_PATH = 'data/test_p.pkl'

ntrain = 23
ntest = 400 // 13 + 1

modes = 2
width = 2
# no params 1049897, 262549
batch_size = 8
learning_rate = 1e-4
epochs = 3000
iterations = epochs*(ntrain//batch_size)

path = 'ns_fourier_3d_N'+str(ntrain)+'_ep' + str(epochs) + '_m' + str(modes) + '_w' + str(width) + '_lr' + str(learning_rate)
path_model = 'model/'+path
path_train_err = 'results/'+path+'train.txt'
path_test_err = 'results/'+path+'test.txt'
path_image = 'image/'+path

runtime = np.zeros(2, )
t1 = default_timer()

sub = 1
WIDTH = 24 // sub # 336 // sub
HEIGHT = 24 // sub # 51 // sub
T_in = 13
T = 13
assert T == T_in  # because later [:,:,:T] would be fed to the model
# assert ntrain*T_in*T == 499 _ 99 = 598
# would do 23 training examples. each with 13 train, 13 test
# 23 * 26 = 598


################################################################
# load data
################################################################

import pickle
def load_pickle(path):
    with open(path, 'rb') as f:
        return pickle.load(f)

def preprocess(pca, data):
    if not pca:
        pca = decomposition.PCA(n_components=WIDTH*HEIGHT).fit(data)
    data = pca.transform(data)
    new_data = data[:data.shape[0] // T * T, :WIDTH*HEIGHT].reshape(T, WIDTH, HEIGHT, -1)
    # new_data = torch.from_numpy(np.array(range(data.shape[0]*WIDTH*HEIGHT), dtype=np.float32).
    #                             reshape(ntrain, WIDTH, HEIGHT, data.shape[0]))
    # slice the train and test
    a = new_data[:,:,:,::2]
    u = new_data[:,:,:,1::2]
    if a.shape[-1] - u.shape[-1] == 1:
       a = a[:,:,:,:-1]
    return pca, torch.from_numpy(a).type(torch.float32), torch.from_numpy(u).type(torch.float32)

pca = None
train = load_pickle(TRAIN_PATH)
val = load_pickle(VAL_PATH)
data = np.concatenate((train, val), axis=0)

pca, train_a, train_u = preprocess(pca, data)

print(train_u.shape)
assert (WIDTH == train_u.shape[-3])
assert (HEIGHT == train_u.shape[-2])

def save_pickle(data, path):
    with open(path, 'wb') as f:
        pickle.dump(data, f)

save_pickle(pca, 'pca.pkl')
_, test_a, test_u = preprocess(pca, load_pickle(TEST_PATH))

train_a = train_a.permute(3,1,2,0)
test_a = test_a.permute(3,1,2,0)

a_normalizer = UnitGaussianNormalizer(train_a)
train_a = a_normalizer.encode(train_a)
test_a = a_normalizer.encode(test_a)
save_pickle(a_normalizer, "unit_gaussian_normalizer.pkl")

train_a = train_a.unsqueeze(3).repeat([1,1,1,T,1])
train_u = train_u.permute(3,1,2,0)
test_a = test_a.unsqueeze(3).repeat([1,1,1,T,1])
test_u = test_u.permute(3,1,2,0)

y_normalizer = UnitGaussianNormalizer(train_u)
train_u = y_normalizer.encode(train_u)

train_loader = torch.utils.data.DataLoader(torch.utils.data.TensorDataset(train_a, train_u), batch_size=batch_size, shuffle=True)
test_loader = torch.utils.data.DataLoader(torch.utils.data.TensorDataset(test_a, test_u), batch_size=batch_size, shuffle=False)

t2 = default_timer()

print('preprocessing finished, time used:', t2-t1)
device = torch.device('cuda')

################################################################
# training and evaluation
################################################################
# model = FNO3d(modes, modes, modes, width)
# print(count_params(model))
# optimizer = torch.optim.Adam(model.parameters(), lr=learning_rate, weight_decay=1e-1)
# scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=iterations)
#
# import tqdm
# min_mse = 2**32
# myloss = LpLoss(size_average=False)
# for ep in range(epochs):
#     model.train()
#     t1 = default_timer()
#     train_mse = 0
#     for x, y in tqdm.tqdm(train_loader):
#
#         optimizer.zero_grad()
#         out = model(x)
#         out = out.view(out.shape[0], WIDTH, HEIGHT, T)
#
#         mse = F.mse_loss(out, y, reduction='mean')
#         mse.backward()
#
#         # y = y_normalizer.decode(y)
#         # out = y_normalizer.decode(out)
#         # l2 = myloss(out, y)
#         # l2.backward()
#
#         optimizer.step()
#         scheduler.step()
#         train_mse += mse.item()
#
#     model.eval()
#     test_mse = 0.0
#     with torch.no_grad():
#         for x, y in test_loader:
#             out = model(x)
#             out = out.view(out.shape[0], WIDTH, HEIGHT, T)
#             out = y_normalizer.decode(out)
#             test_mse += F.mse_loss(out, y).item()
#
#     train_mse /= len(train_loader)
#     test_mse /= ntest
#
#     t2 = default_timer()
#     print("epoch: ", ep, " train loss: ", train_mse, " test loss: ", test_mse)
#     if test_mse < min_mse:
#         min_mse = test_mse
#         torch.save(model, path_model)
#         print("Model saved, min_mse: ", min_mse)

# =============================
model = torch.load(path_model)
pred_original = []
index = 0
test_loss = 0
test_loader = torch.utils.data.DataLoader(torch.utils.data.TensorDataset(test_a, test_u), batch_size=1, shuffle=False)
first = True

x, _ = next(iter(test_loader))
with torch.no_grad():
    for i in range(len(test_loader)*2):
        if first:
            first = False
        else:
            x = pred.unsqueeze(4).repeat([1,1,1,1,T])
        out = model(x)
        out = out.view(out.shape[0], WIDTH, HEIGHT, T)
        out = y_normalizer.decode(out)
        pred = out
        pred_original.append(pca.inverse_transform(out.reshape(WIDTH * HEIGHT, T).permute(1, 0)))


test_loss = 0
with torch.no_grad():
    for x, y in test_loader:
        if first:
            first = False
        else:
            x = pred.unsqueeze(4).repeat([1,1,1,1,T])
        out = model(x)
        out = out.view(out.shape[0], WIDTH, HEIGHT, T)
        out = y_normalizer.decode(out)
        pred = out
        pred_original.append(pca.inverse_transform(out.reshape(WIDTH * HEIGHT, T).permute(1, 0)))

        test_loss += F.mse_loss(out, y, reduction='mean')
        print(index, test_loss)
        index = index + 1
print(torch.sqrt(test_loss))
save_pickle(pred_original, 'pred/pred')
