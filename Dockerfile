FROM python:3.11-slim

WORKDIR /app

# 安装依赖
COPY pyproject.toml README.md ./
COPY src/ src/

RUN pip install --no-cache-dir -e .

# 环境变量
ENV MCP_HOST=0.0.0.0
ENV MCP_PORT=8080

# 暴露端口
EXPOSE 8080

# 启动 HTTP MCP Server
CMD ["trans-mcp-http"]
