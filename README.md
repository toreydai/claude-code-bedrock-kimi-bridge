# Claude Code 接入 Amazon Bedrock Kimi K3

本文记录在 AWS Cloud9/EC2 上使用实例 IAM Role，将 Claude Code 的请求转发到 Amazon Bedrock Kimi K3 的完整配置。

## 1. 架构

Claude Code 原生使用 Anthropic Messages API；Kimi K3 在 Bedrock 上推荐使用 OpenAI-compatible Chat Completions API。两者的流式响应格式不同，因此需要两层本地服务：

```text
Claude Code
    │ Anthropic Messages API
    ▼
本地响应适配器 127.0.0.1:4001
    │ 过滤 Kimi reasoning block，并重新包装 Anthropic SSE
    ▼
LiteLLM 127.0.0.1:4000
    │ Bedrock provider
    ▼
Amazon Bedrock global.moonshotai.kimi-k3
```

AWS 官方建议 Kimi K3 优先使用 Chat Completions/Responses API；Kimi K3 始终会产生 reasoning 内容，这也是需要适配器的原因。

## 2. 前置条件

- Cloud9/EC2 实例绑定了可调用 Bedrock 的 IAM Role。
- 本例中的实例 Role 是 `AdminRole`，Region 是 `us-east-1`。
- AWS CLI、Python 3.11 和 Claude Code 已安装。
- Kimi K3 模型可用，并使用 inference profile：

```text
global.moonshotai.kimi-k3
```

确认身份：

```bash
aws sts get-caller-identity --region us-east-1
```

确认模型：

```bash
aws bedrock list-inference-profiles \
  --region us-east-1 \
  --query 'inferenceProfileSummaries[?contains(inferenceProfileId, `kimi-k3`)]' \
  --output table
```

## 3. 安装 LiteLLM

建议把虚拟环境放在工作区：

```bash
cd /home/ec2-user/environment
uv venv .venv-litellm
uv pip install --python .venv-litellm/bin/python 'litellm[proxy]'
```

## 4. LiteLLM 配置

创建目录：

```bash
mkdir -p /home/ec2-user/environment/.litellm-kimi
```

创建 `config.yaml`：

```yaml
model_list:
  # Claude Code 使用 sonnet 作为客户端模型别名
  - model_name: sonnet
    litellm_params:
      model: bedrock/global.moonshotai.kimi-k3
      aws_region_name: us-east-1
      reasoning_effort: low
      num_retries: 5
      timeout: 300

  # 当前 Claude Code 会把 sonnet 展开成这个模型名
  - model_name: claude-sonnet-5
    litellm_params:
      model: bedrock/global.moonshotai.kimi-k3
      aws_region_name: us-east-1
      reasoning_effort: low
      num_retries: 5
      timeout: 300

litellm_settings:
  # Kimi K3 不支持部分 Claude 参数，例如 temperature
  drop_params: true

general_settings:
  master_key: os.environ/LITELLM_MASTER_KEY
```

创建环境文件：

```bash
cat > /home/ec2-user/environment/.litellm-kimi/litellm.env <<'EOF'
LITELLM_MASTER_KEY=请替换为本机随机字符串
AWS_REGION=us-east-1
AWS_DEFAULT_REGION=us-east-1
EOF

chmod 600 /home/ec2-user/environment/.litellm-kimi/litellm.env
```

不要把 AWS Access Key、Secret Key 或 Session Token 写入该文件。Cloud9 应通过实例 IAM Role 获取临时凭证。

## 5. LiteLLM systemd 服务

创建 `/etc/systemd/system/cloud9-litellm.service`：

```ini
[Unit]
Description=LiteLLM proxy for Bedrock Kimi K3
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ec2-user
WorkingDirectory=/home/ec2-user/environment
EnvironmentFile=/home/ec2-user/environment/.litellm-kimi/litellm.env
ExecStart=/home/ec2-user/environment/.venv-litellm/bin/litellm --config /home/ec2-user/environment/.litellm-kimi/config.yaml --host 127.0.0.1 --port 4000
Restart=always
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

安装并启动：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now cloud9-litellm.service
sudo systemctl status cloud9-litellm.service
```

## 6. Anthropic 响应适配器

LiteLLM 的非流式 Anthropic endpoint 可以返回 Kimi 的最终文本，但其流式响应可能把 Kimi 的 reasoning 内容转换成空文本事件。Claude Code 依赖流式事件，因此需要适配器：

- 对 LiteLLM 使用非流式请求。
- 丢弃 `thinking` block。
- 保留 `text` block。
- 保留并重新包装 `tool_use` block。
- 过滤 Claude Code 动态注入、但 Kimi K3/Bedrock Invoke 不兼容的工具，尤其是所有以 `Artifact` 开头的工具，以及 `AskUserQuestion`、`EnterPlanMode`、`ExitPlanMode`、`EndConversation`、`SendFeedback`、`TaskOutput`。
- 对 Claude Code 输出 Anthropic SSE 事件。

当前适配器文件：

```text
/home/ec2-user/environment/.litellm-kimi/kimi_anthropic_adapter.py
```

它由 FastAPI/Uvicorn 运行，监听 `127.0.0.1:4001`。

如果需要从零创建，可使用以下实现：

```python
import json
import os
import uuid

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI()
UPSTREAM = os.environ.get("KIMI_UPSTREAM", "http://127.0.0.1:4000")
UPSTREAM_KEY = os.environ.get("LITELLM_MASTER_KEY", "")


def normalize_content(content):
    result = []
    for block in content or []:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text" and block.get("text"):
            result.append({"type": "text", "text": block["text"]})
        elif kind == "tool_use":
            result.append({
                "type": "tool_use",
                "id": block.get("id", "tool_" + uuid.uuid4().hex),
                "name": block.get("name", "unknown"),
                "input": block.get("input", {}),
            })
    return result


def anthro_response(upstream):
    usage = upstream.get("usage") or {}
    return {
        "id": upstream.get("id", "msg_" + uuid.uuid4().hex),
        "type": "message",
        "role": "assistant",
        "content": normalize_content(upstream.get("content")),
        "model": upstream.get("model", "claude-sonnet-5"),
        "stop_reason": upstream.get("stop_reason") or "end_turn",
        "stop_sequence": upstream.get("stop_sequence"),
        "usage": {
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
        },
    }


async def call_upstream(payload):
    payload = dict(payload)
    payload["stream"] = False
    headers = {
        "x-api-key": UPSTREAM_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    async with httpx.AsyncClient(timeout=300) as client:
        response = await client.post(
            UPSTREAM + "/v1/messages",
            headers=headers,
            json=payload,
        )
    if response.status_code >= 400:
        return None, JSONResponse(
            status_code=response.status_code,
            content=response.json(),
        )
    return response.json(), None


async def sse_events(message):
    yield "event: message_start\ndata: " + json.dumps({
        "type": "message_start",
        "message": {
            "id": message["id"],
            "type": "message",
            "role": "assistant",
            "content": [],
            "model": message["model"],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {
                "input_tokens": message["usage"]["input_tokens"],
                "output_tokens": 0,
            },
        },
    }) + "\n\n"

    for index, block in enumerate(message["content"]):
        yield "event: content_block_start\ndata: " + json.dumps({
            "type": "content_block_start",
            "index": index,
            "content_block": block,
        }) + "\n\n"

        if block["type"] == "text":
            delta = {
                "type": "content_block_delta",
                "index": index,
                "delta": {
                    "type": "text_delta",
                    "text": block["text"],
                },
            }
        else:
            delta = {
                "type": "content_block_delta",
                "index": index,
                "delta": {
                    "type": "input_json_delta",
                    "partial_json": json.dumps(block["input"]),
                },
            }

        yield "event: content_block_delta\ndata: " + json.dumps(delta) + "\n\n"
        yield "event: content_block_stop\ndata: " + json.dumps({
            "type": "content_block_stop",
            "index": index,
        }) + "\n\n"

    yield "event: message_delta\ndata: " + json.dumps({
        "type": "message_delta",
        "delta": {
            "stop_reason": message["stop_reason"],
            "stop_sequence": None,
        },
        "usage": message["usage"],
    }) + "\n\n"
    yield "event: message_stop\ndata: {\"type\":\"message_stop\"}\n\n"


@app.post("/v1/messages")
async def messages(request: Request):
    payload = await request.json()
    upstream, error = await call_upstream(payload)
    if error:
        return error
    message = anthro_response(upstream)
    if payload.get("stream"):
        return StreamingResponse(
            sse_events(message),
            media_type="text/event-stream",
        )
    return JSONResponse(message)


@app.get("/health")
async def health():
    return {"status": "healthy"}
```

## 7. 适配器 systemd 服务

创建 `/etc/systemd/system/kimi-anthropic-adapter.service`：

```ini
[Unit]
Description=Anthropic response adapter for Bedrock Kimi K3
After=cloud9-litellm.service
Requires=cloud9-litellm.service

[Service]
Type=simple
User=ec2-user
WorkingDirectory=/home/ec2-user/environment
EnvironmentFile=/home/ec2-user/environment/.litellm-kimi/litellm.env
ExecStart=/home/ec2-user/environment/.venv-litellm/bin/uvicorn kimi_anthropic_adapter:app --app-dir /home/ec2-user/environment/.litellm-kimi --host 127.0.0.1 --port 4001
Restart=always
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

安装并启动：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now kimi-anthropic-adapter.service
```

## 8. Claude Code 配置

Claude Code 会校验模型名，因此使用它认识的 `sonnet` 别名；LiteLLM 内部把这个别名映射到 Kimi K3。

在 `~/.bashrc` 中加入：

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:4001
export ANTHROPIC_API_KEY=sk-cloud9-kimi-local
export ANTHROPIC_MODEL=sonnet
```

然后重新打开终端，或执行：

```bash
source ~/.bashrc
```

也可以写入 `~/.claude/settings.json`：

```json
{
  "model": "sonnet",
  "env": {
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:4001",
    "ANTHROPIC_API_KEY": "sk-cloud9-kimi-local",
    "ANTHROPIC_MODEL": "sonnet"
  }
}
```

这里的 `sk-cloud9-kimi-local` 只是本机代理访问口令，不是 AWS 密钥。

## 9. 验证

检查两个服务：

```bash
sudo systemctl is-enabled cloud9-litellm.service kimi-anthropic-adapter.service
sudo systemctl is-active cloud9-litellm.service kimi-anthropic-adapter.service
```

健康检查：

```bash
curl http://127.0.0.1:4000/health/readiness
curl http://127.0.0.1:4001/health
```

Claude Code 端到端测试：

```bash
claude -p 'Reply with exactly KIMI_K3_OK' --output-format text
```

工具调用测试：

```bash
mkdir -p /tmp/kimi-k3-tool-test
cd /tmp/kimi-k3-tool-test
claude -p '请使用写文件工具创建 test.txt，内容为 TOOL_OK。完成后回复 DONE。' --output-format text
cat test.txt
```

预期看到：

```text
TOOL_OK
```

## 10. 日志和故障排查

查看 LiteLLM 日志：

```bash
sudo journalctl -u cloud9-litellm.service -f
```

查看适配器日志：

```bash
sudo journalctl -u kimi-anthropic-adapter.service -f
```

常见问题：

### `NoCredentials`

检查实例是否绑定 IAM Role：

```bash
curl http://169.254.169.254/latest/meta-data/iam/security-credentials/
aws sts get-caller-identity --region us-east-1
```

### `on-demand throughput isn’t supported`

Kimi K3 不能直接使用基础模型 ID，必须使用 inference profile：

```text
global.moonshotai.kimi-k3
```

### Claude Code 返回空文本

确认 Claude Code 的地址是适配器端口 `4001`，而不是 LiteLLM 原始端口 `4000`：

```bash
echo "$ANTHROPIC_BASE_URL"
```

应该是：

```text
http://127.0.0.1:4001
```

### `503 API Error` 或 Bedrock 500

如果日志出现：

```text
litellm.ServiceUnavailableError: BedrockException
The system encountered an unexpected error during processing.
```

并且 Claude Code 请求包含 `Artifact`、`ArtifactComments`、`ArtifactData` 等工具，这是 Kimi K3 经 LiteLLM 转换 Claude 工具定义时的兼容性问题，不是 IAM 权限问题。

适配器会过滤所有 `Artifact*` 工具，以及以下不兼容工具：

```text
AskUserQuestion
EndConversation
EnterPlanMode
ExitPlanMode
SendFeedback
TaskOutput
```

核心的 `Bash`、`Read`、`Edit`、`Write`、`Agent`、`WebSearch` 等工具仍会保留。

LiteLLM 配置建议保留重试和超时：

```yaml
num_retries: 5
timeout: 300
```

修改后重启服务，并重新打开 Claude Code 会话：

```bash
sudo systemctl restart cloud9-litellm.service
sudo systemctl restart kimi-anthropic-adapter.service
source ~/.bashrc
claude
```

### Kimi 输出太慢或太贵

Kimi K3 始终会进行 reasoning。可以在 LiteLLM 配置中使用：

```yaml
reasoning_effort: low
```

但不能完全关闭 K3 的 reasoning。

## 11. 安全注意事项

- LiteLLM 和适配器只监听 `127.0.0.1`，不应直接暴露公网。
- 不要把 AWS 临时凭证写入配置文件。
- 不要把 `LITELLM_MASTER_KEY` 提交到 Git。
- Cloud9 的公网 IP 可能变化，安全组入站规则应尽量限制为个人公网 IP `/32`。
- Claude Code 警告 `claude.ai connectors are disabled` 是预期行为，因为当前使用的是本地 Bedrock 代理，不是 Claude.ai 登录。

## 12. 停止或卸载

停止服务但保留配置：

```bash
sudo systemctl disable --now kimi-anthropic-adapter.service
sudo systemctl disable --now cloud9-litellm.service
```

卸载 systemd 服务：

```bash
sudo rm /etc/systemd/system/kimi-anthropic-adapter.service
sudo rm /etc/systemd/system/cloud9-litellm.service
sudo systemctl daemon-reload
```

## 13. 当前已验证结果

本方案已在 Cloud9 上验证：

- Bedrock Role：`AdminRole`
- Region：`us-east-1`
- Kimi inference profile：`global.moonshotai.kimi-k3`
- LiteLLM：`127.0.0.1:4000`
- Anthropic 适配器：`127.0.0.1:4001`
- Claude Code 文本请求：成功
- Claude Code 文件编辑工具调用：成功
- Claude Code 动态 `Artifact*` 工具过滤：成功
- LiteLLM Bedrock 重试次数：5 次
