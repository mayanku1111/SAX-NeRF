import torch
import torch.nn as nn
import torch.nn.functional as F
from torch import einsum
import math
import warnings
from torch.nn.init import _calculate_fan_in_and_fan_out
from torchvision import models
from torch.nn.functional import relu

from pdb import set_trace as stx

def _no_grad_trunc_normal_(tensor, mean, std, a, b):
    def norm_cdf(x):
        return (1. + math.erf(x / math.sqrt(2.))) / 2.

    if (mean < a - 2 * std) or (mean > b + 2 * std):
        warnings.warn("mean is more than 2 std from [a, b] in nn.init.trunc_normal_. "
                      "The distribution of values may be incorrect.",
                      stacklevel=2)

    with torch.no_grad():
        l = norm_cdf((a - mean) / std)
        u = norm_cdf((b - mean) / std)
        tensor.uniform_(2 * l - 1, 2 * u - 1)
        tensor.erfinv_()
        tensor.mul_(std * math.sqrt(2.))
        tensor.add_(mean)
        tensor.clamp_(min=a, max=b)
        return tensor

def trunc_normal_(tensor, mean=0., std=1., a=-2., b=2.):
    return _no_grad_trunc_normal_(tensor, mean, std, a, b)



class PreNorm(nn.Module):
    def __init__(self, dim, fn):
        super().__init__()
        self.fn = fn
        self.norm = nn.LayerNorm(dim)

    def forward(self, x, *args, **kwargs):
        '''
            x.shape = b, h, w, c
            若 fn 为卷积层，则 需要改变维度到 b, c, h, w
            对应到 MLP 中就是 b, N, c
        '''
        x = self.norm(x)
        return self.fn(x, *args, **kwargs)

class GELU(nn.Module):
    def forward(self, x):
        return F.gelu(x)

# 将 point 的 batch 转换为 line_segment 的 batch
def ray_partition(x, line_size):
    """
    x: [ N_ray * N_samples, c ]
    line_batch: [ N_ray * N_samples // line_size, line_size, c ]
    """
    # stx()
    n,c = x.shape       # (N_ray*N_samples, c)
    # if n*c == 13107200:
    #     stx()
    line_bacth = x.view(n // line_size, line_size, c) # (N_ray*N_samples, c) -> (N_ray*N_samples // line_size, line_size, c)
    return line_bacth

# 将 line_segment 的 batch 转换为 point 的 batch
def ray_merge(x):
    """
    x: [N_ray*N_samples // line_size, line_size, c]
    out: (N_ray*N_samples, c)

    x: [b*hw/n,n,c], where n = window_size[0]*window_size[1]
    return out: [b h w c]
    """
    line_bacth_num, line_size, c = x.shape
    point_batch = x.view(line_bacth_num * line_size, c)
    return point_batch

class LineAttention(nn.Module):
    def __init__(
        self,
        dim,
        line_size=24,
        dim_head = 64,
        heads = 8
    ):
        super().__init__()

        self.dim = dim                  # dim = 输入维度 = 24
        self.heads = heads
        self.scale = dim_head ** -0.5
        self.line_size = line_size

        # position embedding
        seq_l = line_size       # 24
        self.pos_emb = nn.Parameter(torch.Tensor(1, heads, seq_l , seq_l)) # [1, 8, 24, 24]
        trunc_normal_(self.pos_emb)

        inner_dim = dim_head * heads        # 64 * 8
        self.to_q = nn.Linear(dim, inner_dim, bias=False)   # c -> inner_dim
        self.to_kv = nn.Linear(dim, inner_dim * 2, bias=False) # c -> 2 * inner_dim
        self.to_out = nn.Linear(inner_dim, dim)     # inner_dim -> c

    def forward(self,x):
        """
        x: [n,c]
        return out: [n,c]
        n = N_ray * N_samples
        """
        n,c = x.shape
        l_size = self.line_size
        

        # shift the feature map by half window size
        # if self.shift_size[0] > 0:
        #     x = torch.roll(x, shifts=(-self.shift_size[0], -self.shift_size[1]), dims=(1, 2))

        # Reshape to (B,N,C), where N = window_size[0]*window_size[1] is the length of sentence
        # x_inp = rearrange(x, 'b (h b0) (w b1) c -> (b h w) (b0 b1) c', b0=w_size[0], b1=w_size[1])
        # x_inp = x.view(x.shape[0]*x.shape[1]//w_size[0]*x.shape[2]//w_size[1], w_size[0]*w_size[1], x.shape[3])
        '''
            point batch 转成 line batch. 实际计算时是 line batch. 
            [N_ray * N_samples, c] -> [N_ray * N_samples // line_size, line_size, c]
        '''
        x_inp = ray_partition(x, line_size=l_size)

        # produce query, key and value
        # .chunk() 函数表示沿着维度进行分割
        q = self.to_q(x_inp)                        # [b*hw/n, n, c] -> [b*hw/n, n, inner_dim]
        k, v = self.to_kv(x_inp).chunk(2, dim=-1)   # [b*hw/n, n, c] -> [b*hw/n, n, 2*inner_dim] -> 2*[b*hw/n, n, inner_dim]

        # split heads
        # q, k, v = map(lambda t: rearrange(t, 'b n (h d) -> b h n d', h=self.heads), (q, k, v))
        # q, k, v = map(lambda t: t.contiguous().view(t.shape[0],self.heads,t.shape[1],t.shape[2]//self.heads), (q, k, v))
        '''
            对通道维度分head, 并交换后两个维度方便计算
            内部定义一个匿名函数, 然后用 map 把变量 (q, k, v) 传进去
            q, k, v: [N_ray * N_samples // line_size, line_size, inner_dim]
            -> [N_ray * N_samples // line_size, line_size, heads, dim_head]
            -> [N_ray * N_samples // line_size, heads, line_size, dim_head]
        '''
        q, k, v = map(lambda t: t.contiguous().view(t.shape[0], t.shape[1], self.heads, t.shape[2] // self.heads).permute(0,2,1,3),
                      (q, k, v))


        # scale
        q *= self.scale     # q / squart(d)

        # attention
        '''
            Q x K.T
            [N_ray * N_samples // line_size, heads, line_size, dim_head]
            x [N_ray * N_samples // line_size, heads, dim_head, line_size]
            out: [N_ray * N_samples // line_size, heads, line_size, line_size]
        '''
        sim = einsum('b h i d, b h j d -> b h i j', q, k)       # Q, K 矩阵相乘
        sim = sim + self.pos_emb
        attn = sim.softmax(dim=-1)

        # aggregate
        '''
            attn x V
            [N_ray * N_samples // line_size, heads, line_size, line_size]
             x [N_ray * N_samples // line_size, heads, line_size, dim_head]
            out: [N_ray * N_samples // line_size, heads, line_size, dim_head]
        '''
        out = einsum('b h i j, b h j d -> b h i d', attn, v)    # attn 和 v 相乘

        # merge and combine heads
        # out = rearrange(out, 'b h n d -> b n (h d)')
        # out = out.view(out.shape[0],out.shape[2],-1)
        '''
            合并head, 并将通道维度由 inner_dim 转成 c
            out: [N_ray * N_samples // line_size, heads, line_size, dim_head]
            -> [N_ray * N_samples // line_size, line_size, heads, dim_head]
            -> [N_ray * N_samples // line_size, line_size, inner_dim]
            -> [N_ray * N_samples // line_size, line_size, c]
        '''
        out = out.permute(0,2,1,3).contiguous().view(out.shape[0],out.shape[2],-1)
        out = self.to_out(out)

        # merge windows back to original feature map
        # out = rearrange(out, '(b h w) (b0 b1) c -> b (h b0) (w b1) c', h=h//w_size[0], w=w//w_size[1],b0=w_size[0])
        # out = out.view(out.shape[0]//(h//w_size[0])//(w//w_size[1]), h, w, c)
        
        '''
            把 window 的 batch 重新转换成 feature 的 batch
        '''
        out = ray_merge(out)

        # inverse shift the feature map by half window size
        # if self.shift_size[0] > 0:
        #     out = torch.roll(out, shifts=(self.shift_size[0], self.shift_size[1]), dims=(1, 2))

        return out


class FFN(nn.Module):
    def __init__(self, dim, mult=4):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(dim, dim * mult,bias=False),
            GELU(),
            nn.Linear(dim*mult, dim*mult, bias=False),
            GELU(),
            nn.Linear(dim * mult, dim, bias=False),
        )

    def forward(self, x):
        """
        x: [ N_ray * N_sample, c ]
        return out: [ N_ray * N_sample, c ]
        """
        out = self.net(x)
        return out

class Line_Attention_Blcok(nn.Module):
    def __init__(
            self,
            dim,
            line_size=24,
            dim_head=32,
            heads=8,
            num_blocks = 1
    ):
        super().__init__()
        self.blocks = nn.ModuleList([])
        for _ in range(num_blocks):
            self.blocks.append(nn.ModuleList([
                PreNorm(dim, LineAttention(dim=dim,line_size=line_size,dim_head=dim_head,heads=heads)),
                PreNorm(dim, FFN(dim=dim))
            ]))

    def forward(self, x):
        """
        x: [n_ray*n_sample, c]
        return out: [n_ray*n_sample, c]
        """
        for (attn, ff) in self.blocks:
            x = attn(x) + x
            x = ff(x) + x
        return x


class UNet(nn.Module):
    def __init__(self, n_class):
        super().__init__()
        
        # Encoder
        # In the encoder, convolutional layers with the Conv2d function are used to extract features from the input image. 
        # Each block in the encoder consists of two convolutional layers followed by a max-pooling layer, with the exception of the last block which does not include a max-pooling layer.
        # -------
        # input: 572x572x3
        self.e11 = nn.Conv2d(3, 64, kernel_size=3, padding=1) # output: 570x570x64
        self.e12 = nn.Conv2d(64, 64, kernel_size=3, padding=1) # output: 568x568x64
        self.pool1 = nn.MaxPool2d(kernel_size=2, stride=2) # output: 284x284x64

        # input: 284x284x64
        self.e21 = nn.Conv2d(64, 128, kernel_size=3, padding=1) # output: 282x282x128
        self.e22 = nn.Conv2d(128, 128, kernel_size=3, padding=1) # output: 280x280x128
        self.pool2 = nn.MaxPool2d(kernel_size=2, stride=2) # output: 140x140x128

        # input: 140x140x128
        self.e31 = nn.Conv2d(128, 256, kernel_size=3, padding=1) # output: 138x138x256
        self.e32 = nn.Conv2d(256, 256, kernel_size=3, padding=1) # output: 136x136x256
        self.pool3 = nn.MaxPool2d(kernel_size=2, stride=2) # output: 68x68x256

        # input: 68x68x256
        self.e41 = nn.Conv2d(256, 512, kernel_size=3, padding=1) # output: 66x66x512
        self.e42 = nn.Conv2d(512, 512, kernel_size=3, padding=1) # output: 64x64x512
        self.pool4 = nn.MaxPool2d(kernel_size=2, stride=2) # output: 32x32x512

        # input: 32x32x512
        self.e51 = nn.Conv2d(512, 1024, kernel_size=3, padding=1) # output: 30x30x1024
        self.e52 = nn.Conv2d(1024, 1024, kernel_size=3, padding=1) # output: 28x28x1024


        # Decoder
        self.upconv1 = nn.ConvTranspose2d(1024, 512, kernel_size=2, stride=2)
        self.d11 = nn.Conv2d(1024, 512, kernel_size=3, padding=1)
        self.d12 = nn.Conv2d(512, 512, kernel_size=3, padding=1)

        self.upconv2 = nn.ConvTranspose2d(512, 256, kernel_size=2, stride=2)
        self.d21 = nn.Conv2d(512, 256, kernel_size=3, padding=1)
        self.d22 = nn.Conv2d(256, 256, kernel_size=3, padding=1)

        self.upconv3 = nn.ConvTranspose2d(256, 128, kernel_size=2, stride=2)
        self.d31 = nn.Conv2d(256, 128, kernel_size=3, padding=1)
        self.d32 = nn.Conv2d(128, 128, kernel_size=3, padding=1)

        self.upconv4 = nn.ConvTranspose2d(128, 64, kernel_size=2, stride=2)
        self.d41 = nn.Conv2d(128, 64, kernel_size=3, padding=1)
        self.d42 = nn.Conv2d(64, 64, kernel_size=3, padding=1)

        # Output layer
        self.outconv = nn.Conv2d(64, n_class, kernel_size=1)


    def forward(self, x):
        # Encoder
        xe11 = relu(self.e11(x))
        xe12 = relu(self.e12(xe11))
        xp1 = self.pool1(xe12)

        xe21 = relu(self.e21(xp1))
        xe22 = relu(self.e22(xe21))
        xp2 = self.pool2(xe22)

        xe31 = relu(self.e31(xp2))
        xe32 = relu(self.e32(xe31))
        xp3 = self.pool3(xe32)

        xe41 = relu(self.e41(xp3))
        xe42 = relu(self.e42(xe41))
        xp4 = self.pool4(xe42)

        xe51 = relu(self.e51(xp4))
        xe52 = relu(self.e52(xe51))
        
        # Decoder
        xu1 = self.upconv1(xe52)
        xu11 = torch.cat([xu1, xe42], dim=1)
        xd11 = relu(self.d11(xu11))
        xd12 = relu(self.d12(xd11))

        xu2 = self.upconv2(xd12)
        xu22 = torch.cat([xu2, xe32], dim=1)
        xd21 = relu(self.d21(xu22))
        xd22 = relu(self.d22(xd21))

        xu3 = self.upconv3(xd22)
        xu33 = torch.cat([xu3, xe22], dim=1)
        xd31 = relu(self.d31(xu33))
        xd32 = relu(self.d32(xd31))

        xu4 = self.upconv4(xd32)
        xu44 = torch.cat([xu4, xe12], dim=1)
        xd41 = relu(self.d41(xu44))
        xd42 = relu(self.d42(xd41))

        # Output layer
        out = self.outconv(xd42)

        return out

class Lineformer_no_encoder(nn.Module):
    def __init__(self, bound=0.2, num_layers=8, hidden_dim=256, skips=[4], out_dim=1, 
                    last_activation="sigmoid", line_size=32, dim_head=32, heads=8, num_blocks = 1,unet_out_dim=3):
        super().__init__()
        self.nunm_layers = num_layers
        self.hidden_dim = hidden_dim
        self.skips = skips
        self.bound = bound
        self.unet = UNet(unet_out_dim)
        self.in_dim = 32
        
        # Linear layers
        # 实例化一些全连接层 —> 实例化一些Line_Attention_Block
        self.layers = nn.ModuleList(
            [self.unet] + [Line_Attention_Blcok(dim=hidden_dim, line_size=line_size, dim_head=dim_head, heads=heads, num_blocks=num_blocks)
            for i in range(1, num_layers-1) if i not in skips]
        )

        # Activations
        self.activations = nn.ModuleList([nn.LeakyReLU() for i in range(0, num_layers-1, 1)])
        if last_activation == "sigmoid":
            self.activations.append(nn.Sigmoid())
        elif last_activation == "relu":
            self.activations.append(nn.LeakyReLU())
        else:
            raise NotImplementedError("Unknown last activation")

    def forward(self, x):
        
        x = self.unet(x)
        for i in range(1,len(self.layers)):

            layer = self.layers[i]
            x = layer(x)

            x = self.activations[i-1](x) 
         return x

class Lineformer(nn.Module):
    def __init__(self, bound=0.2, num_layers=8, hidden_dim=256, skips=[4], out_dim=1, 
                    last_activation="sigmoid", line_size=16, dim_head=32, heads=8, num_blocks = 1,unet_out_dim=3):
        super().__init__()
        self.nunm_layers = num_layers
        self.hidden_dim = hidden_dim
        self.skips = skips
        self.bound = bound
        self.in_dim = hidden_dim
        self.unet = UNet(n_class=unet_out_dim)                
        
        # Linear layers
        # 实例化一些全连接层 —> 实例化一些Line_Attention_Block
        self.layers = nn.ModuleList(
            [self.unet] + [Line_Attention_Blcok(dim=hidden_dim, line_size=line_size, dim_head=dim_head, heads=heads, num_blocks = num_blocks) 
            for i in range(1, num_layers-1) if i not in skips]
        )

        # Activations
        self.activations = nn.ModuleList([nn.LeakyReLU() for i in range(0, num_layers-1, 1)])
        if last_activation == "sigmoid":
            self.activations.append(nn.Sigmoid())
        elif last_activation == "relu":
            self.activations.append(nn.LeakyReLU())
        else:
            raise NotImplementedError("Unknown last activation")

    
    def forward(self, x):
        x = self.encoder(x, self.bound)
        x = self.unet(x)
        for i in range(1, len(self.layers)):
            layer = self.layers[i]
            x = layer(x)
            x = self.activations[i-1](x)
    
        return x

if __name__ == '__main__':
    from fvcore.nn import FlopCountAnalysis
    device = torch.device("cuda:1" if torch.cuda.is_available() else "cpu")
    model = Lineformer_no_encoder(num_layers=4,hidden_dim=32,skips=[2],out_dim=1,last_activation="sigmoid",bound=0.3).to(device)
    inputs = torch.randn((1024*192, 32)).to(device)
    flops = FlopCountAnalysis(model,inputs)
    n_param = sum([p.nelement() for p in model.parameters()])  # 所有参数数量
    print(f'GMac:{flops.total()/(1024*1024*1024)}')
    print(f'Params:{n_param}')
