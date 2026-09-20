# Claude Code Bedrock Kimi Bridge

通过 LiteLLM 和 Anthropic 响应适配器，将 Claude Code 接入 Amazon Bedrock Kimi K3。

## 架构

```text
Claude Code
  → Anthropic Adapter :4001
  → LiteLLM :4000
  → Bedrock global.moonshotai.kimi-k3
```

适配器负责过滤 Kimi K3 的 reasoning block，并将响应转换为 Claude Code 需要的 Anthropic SSE 格式。同时会过滤不兼容的 `Artifact*` 等 Claude Code 动态工具，保留 Bash、Read、Edit、Write 等核心工具。

## 前置条件

- Cloud9/EC2 实例绑定可调用 Bedrock 的 IAM Role
- AWS CLI、Python 3.11、`uv` 和 Claude Code
- Kimi K3 inference profile 可用

确认 AWS 身份：

```bash
aws sts get-caller-identity --region us-east-1
```

确认 Kimi K3：

```bash
aws bedrock list-inference-profiles \
  --region us-east-1 \
  --query 'inferenceProfileSummaries[?contains(inferenceProfileId, `kimi-k3`)]' \
  --output table
```

## 文件说明

| 文件 | 用途 |
|---|---|
| [`src/kimi_anthropic_adapter.py`](src/kimi_anthropic_adapter.py) | Anthropic SSE 响应适配器 |
| [`deploy/config.yaml`](deploy/config.yaml) | LiteLLM 模型路由和重试配置 |
| [`deploy/cloud9-litellm.service`](deploy/cloud9-litellm.service) | LiteLLM systemd 服务 |
| [`deploy/kimi-anthropic-adapter.service`](deploy/kimi-anthropic-adapter.service) | 适配器 systemd 服务 |
| [`deploy/litellm.env.example`](deploy/litellm.env.example) | 环境变量模板 |

## 安装 LiteLLM

```bash
cd /home/ec2-user/environment
uv venv .venv-litellm
uv pip install --python .venv-litellm/bin/python 'litellm[proxy]'
```

## 配置环境变量

```bash
cd /home/ec2-user/environment/claude-code-bedrock-kimi-bridge
```

```bash
mkdir -p /home/ec2-user/environment/.litellm-kimi
cp deploy/litellm.env.example /home/ec2-user/environment/.litellm-kimi/litellm.env
chmod 600 /home/ec2-user/environment/.litellm-kimi/litellm.env
```

编辑 `litellm.env`，至少设置一个本机代理密钥：

```text
LITELLM_MASTER_KEY=替换为随机字符串
AWS_REGION=us-east-1
AWS_DEFAULT_REGION=us-east-1
```

不要写入 AWS Access Key、Secret Key 或 Session Token；Cloud9 应使用实例 IAM Role。

## 安装配置文件

```bash
sudo install -m 644 deploy/cloud9-litellm.service \
  /etc/systemd/system/cloud9-litellm.service
sudo install -m 644 deploy/kimi-anthropic-adapter.service \
  /etc/systemd/system/kimi-anthropic-adapter.service

sudo mkdir -p /home/ec2-user/environment/.litellm-kimi
cp deploy/config.yaml /home/ec2-user/environment/.litellm-kimi/config.yaml
cp src/kimi_anthropic_adapter.py \
  /home/ec2-user/environment/.litellm-kimi/kimi_anthropic_adapter.py

sudo systemctl daemon-reload
sudo systemctl enable --now cloud9-litellm.service
sudo systemctl enable --now kimi-anthropic-adapter.service
```

## Claude Code 配置

加入 `~/.bashrc`：

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:4001
export ANTHROPIC_API_KEY=你的本机代理密钥
export ANTHROPIC_MODEL=sonnet
```

加载配置：

```bash
source ~/.bashrc
claude
```

`sonnet` 只是 Claude Code 能识别的客户端别名，实际后端模型由 `deploy/config.yaml` 映射为 Kimi K3。

## 验证

```bash
sudo systemctl is-active cloud9-litellm.service kimi-anthropic-adapter.service
curl http://127.0.0.1:4000/health/readiness
curl http://127.0.0.1:4001/health
claude -p 'Reply with exactly KIMI_K3_OK' --output-format text
```

工具调用测试：

```bash
mkdir -p /tmp/kimi-k3-tool-test
cd /tmp/kimi-k3-tool-test
claude -p '请使用写文件工具创建 test.txt，内容为 TOOL_OK。完成后回复 DONE。'
cat test.txt
```

## 常见问题

### `503 API Error` / Bedrock 500

如果日志出现：

```text
litellm.ServiceUnavailableError: BedrockException
The system encountered an unexpected error during processing.
```

通常是 Claude Code 动态工具与 Kimi K3 的转换兼容问题。适配器会过滤以下工具：

```text
Artifact*
AskUserQuestion
EndConversation
EnterPlanMode
ExitPlanMode
SendFeedback
TaskOutput
```

LiteLLM 已配置 5 次重试和 300 秒超时。修改配置后重启：

```bash
sudo systemctl restart cloud9-litellm.service
sudo systemctl restart kimi-anthropic-adapter.service
```

已有 Claude Code 会话需要退出并重新打开。

### 查看日志

```bash
sudo journalctl -u cloud9-litellm.service -f
sudo journalctl -u kimi-anthropic-adapter.service -f
```

### Kimi K3 输出慢或 token 消耗高

Kimi K3 始终启用 reasoning，可在 `deploy/config.yaml` 中使用：

```yaml
reasoning_effort: low
```

但不能完全关闭 reasoning。

## 安全注意事项

- 两个服务默认只监听 `127.0.0.1`。
- 不要提交真实的 `litellm.env`。
- 不要提交 AWS 密钥或临时凭证。
- Cloud9 公网 IP 可能变化，安全组规则应限制为个人 IP `/32`。

## 停止服务

```bash
sudo systemctl disable --now kimi-anthropic-adapter.service
sudo systemctl disable --now cloud9-litellm.service
```
