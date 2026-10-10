#!/data/data/com.termux/files/usr/bin/bash
echo "====================================="
echo "       Z-Agent API 快速切换工具       "
echo "====================================="
echo "1. DeepSeek 官方 (deepseek-flash)"
echo "2. 自定义 (手动输入)"
read -p "请选择 (1/2): " choice

if [ "$choice" = "1" ]; then
    url="https://api.deepseek.com/chat/completions"
    model="deepseek-flash"
else
    read -p "请输入 API URL (如 https://api.xxx.com/v1/chat/completions): " url
    read -p "请输入模型名: " model
fi

read -p "请输入 API Key: " api_key

# 自动修改 zagent.py 中的地址和模型（用 sed 安全替换）
sed -i "s|^BASE_URL = .*|BASE_URL = os.environ.get(\"RAIN_BASE_URL\", \"$url\")|" ~/zagent.py
sed -i "s|^MODEL = .*|MODEL = os.environ.get(\"RAIN_MODEL\", \"$model\")|" ~/zagent.py

# 把密钥写入配置文件
mkdir -p ~/mind
echo "{\"api_key\": \"$api_key\"}" > ~/mind/config.json

# 同时把地址和模型写入环境变量文件（最关键一步，防止401）
echo "export RAIN_BASE_URL=\"$url\"" > ~/.rain_env
echo "export RAIN_MODEL=\"$model\"" >> ~/.rain_env
echo "export DEEPSEEK_API_KEY=\"$api_key\"" >> ~/.rain_env

echo "✅ 配置已保存！以后请直接运行 ~/start_agent.sh 启动。"
