import os
import argparse
import time
from pathlib import Path
import torch
import torch.nn as nn
from torch.utils.data import DataLoader, Dataset, random_split
from torchvision import transforms, models
import torch.backends.cudnn as cudnn
from tqdm import tqdm
import wandb
from PIL import Image
from Unet_Resnet import ResNetUNet

# ---------------------------
# 自定义数据集
# ---------------------------
class SegmentationDataset(Dataset):
    def __init__(self, images_dir, masks_dir, image_transform=None, mask_transform=None):
        """
        images_dir: 存放 RGB 图像的文件夹路径 (e.g. dataset/train/images)
        masks_dir: 存放 mask 图像的文件夹路径 (e.g. dataset/train/masks)
        image_transform: 对图像的预处理（例如 Resize、ToTensor、Normalize）
        mask_transform: 对 mask 的预处理（例如 Resize、ToTensor）
        """
        self.images_dir = images_dir
        self.masks_dir = masks_dir
        self.image_transform = image_transform
        self.mask_transform = mask_transform
        
        self.image_files = sorted(os.listdir(images_dir))
    
    def __len__(self):
        return len(self.image_files)
    
    def __getitem__(self, idx):
        image_path = os.path.join(self.images_dir, self.image_files[idx])
        mask_path = os.path.join(self.masks_dir, self.image_files[idx])  # 假设 mask 文件名与 image 相同
        image = Image.open(image_path).convert('RGB')
        mask = Image.open(mask_path).convert('L')  # 灰度图
        
        if self.image_transform:
            image = self.image_transform(image)
        if self.mask_transform:
            mask = self.mask_transform(mask)
        
        # 将 mask 二值化（假设原始 mask 像素值为0或255）
        mask = (mask > 0.5).float()
        return image, mask

# ---------------------------
# 主函数
# ---------------------------
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--images_dir', type=str, default='/home/hongjia/CV_H/dataset/train/train/images', help="训练图像文件夹路径")
    parser.add_argument('--masks_dir', type=str, default='/home/hongjia/CV_H/dataset/train/train/masks', help="mask 文件夹路径")
    parser.add_argument('--log_dir', type=str, default='./logs', help="日志保存路径")
    parser.add_argument('--checkpoint_dir', type=str, default='./checkpoints', help="模型 checkpoint 保存路径")
    parser.add_argument('--batch_size', type=int, default=4, help="batch 大小")
    parser.add_argument('--num_epochs', type=int, default=50, help="训练总轮数")
    parser.add_argument('--val_split', type=float, default=0.2, help="验证集比例（0~1之间）")
    parser.add_argument('--wandb_project', type=str, default='segmentation_project', help="wandb 项目名称")
    args = parser.parse_args()

    # 设置 cudnn
    cudnn.enabled = True
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    
    # 创建 checkpoint 保存文件夹
    checkpoint_dir = Path(args.checkpoint_dir)
    checkpoint_dir.mkdir(exist_ok=True, parents=True)
    
    # 初始化 wandb
    wandb.init(project=args.wandb_project, config=vars(args))
    
    # 图像预处理（注意 mask 单独处理）
    image_transforms = transforms.Compose([
        transforms.Resize((256, 256)),
        transforms.ToTensor(),
        transforms.Normalize(mean=[0.485, 0.456, 0.406],
                             std=[0.229, 0.224, 0.225])
    ])
    
    mask_transforms = transforms.Compose([
        transforms.Resize((256, 256)),
        transforms.ToTensor()  # 转为 [0,1] 之间，后续二值化
    ])
    
    # 创建数据集
    full_dataset = SegmentationDataset(
        images_dir=args.images_dir,
        masks_dir=args.masks_dir,
        image_transform=image_transforms,
        mask_transform=mask_transforms
    )
    
    # 划分训练集和验证集
    total_size = len(full_dataset)
    val_size = int(total_size * args.val_split)
    train_size = total_size - val_size
    train_dataset, val_dataset = random_split(full_dataset, [train_size, val_size])
    
    # DataLoader
    train_loader = DataLoader(
        dataset=train_dataset,
        batch_size=args.batch_size,
        shuffle=True,
        num_workers=4,
        pin_memory=True
    )
    
    val_loader = DataLoader(
        dataset=val_dataset,
        batch_size=args.batch_size,
        shuffle=False,
        num_workers=4,
        pin_memory=True
    )
    
    # 初始化模型、损失函数和优化器
    model = ResNetUNet(out_channels=1).to(device)
    criterion = nn.BCEWithLogitsLoss()
    optimizer = torch.optim.Adam(model.parameters(), lr=1e-3)
    
    # 如果你想让 wandb 记录模型结构和梯度，可以使用 wandb.watch()
    # wandb.watch(model, criterion, log="all", log_freq=10)
    
    # 定义一个全局 step 计数器，用来记录 batch 级别日志
    global_step = 0
    
    # 开始训练
    for epoch in range(args.num_epochs):
        model.train()
        train_loss = 0.0
        
        # -----------------------------
        # 训练循环（batch 级别 logging）
        # -----------------------------
        for images, masks in tqdm(train_loader, desc=f"Epoch {epoch+1}/{args.num_epochs} - Training"):
            images = images.to(device)
            masks = masks.to(device)
            
            optimizer.zero_grad()
            outputs = model(images)
            loss = criterion(outputs, masks)
            loss.backward()
            optimizer.step()
            
            # 累积 epoch 内的 loss
            train_loss += loss.item() * images.size(0)
            
            # 每个 batch 结束后 log 当前 batch 的 loss
            wandb.log({"train_loss_batch": loss.item()}, step=global_step)
            global_step += 1
        
        # 计算 epoch 平均 loss
        train_loss /= len(train_dataset)
        wandb.log({"train_loss_epoch": train_loss, "epoch": epoch + 1})
        print(f"Epoch [{epoch+1}/{args.num_epochs}], Train Loss: {train_loss:.4f}")
        
        # 每5个 epoch 进行一次验证并保存模型 checkpoint
        if (epoch + 1) % 5 == 0:
            model.eval()
            val_loss = 0.0
            with torch.no_grad():
                for images, masks in tqdm(val_loader, desc=f"Epoch {epoch+1}/{args.num_epochs} - Validation"):
                    images = images.to(device)
                    masks = masks.to(device)
                    outputs = model(images)
                    loss = criterion(outputs, masks)
                    val_loss += loss.item() * images.size(0)
            
            val_loss /= len(val_dataset)
            wandb.log({"val_loss": val_loss, "epoch": epoch + 1})
            print(f"Epoch [{epoch+1}/{args.num_epochs}], Validation Loss: {val_loss:.4f}")
            
            # 保存模型 checkpoint
            checkpoint_path = os.path.join(args.checkpoint_dir, f"checkpoint_epoch_{epoch+1}.pth")
            torch.save(model.state_dict(), checkpoint_path)
            print(f"Checkpoint saved at {checkpoint_path}")
    
    print("训练完成。")

if __name__ == '__main__':
    main()
