#!/data/data/com.termux/files/usr/bin/bash
# Z-Agent 一键部署脚本（Termux 专用）

echo "正在为 Z-Agent 部署环境，请稍候..."

# 1. 更新并安装基础依赖
pkg update -y && pkg upgrade -y
pkg install python git unzip -y
pip install requests

# 2. 克隆项目到当前目录（如果已经存在则拉取更新）
if [ -d "Z-agent-termux" ]; then
    echo "项目已存在，正在拉取最新代码..."
    cd Z-agent-termux && git pull
else
    git clone https://github.com/zxxzxx1314/Z-agent-termux.git
    cd Z-agent-termux
fi

# 3. 赋予脚本执行权限
chmod +x start_agent.sh switch_api.sh

echo ""
echo "✅ 部署完成！"
echo "请执行以下命令启动："
echo "cd Z-agent-termux && ./switch_api.sh  （配置您的 API Key）"
echo "./start_agent.sh                     （启动 Z-Agent）"
