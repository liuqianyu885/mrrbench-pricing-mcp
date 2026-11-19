FROM python:3.12-slim
WORKDIR /app
COPY pricing_mcp.py ./
COPY sample_data ./sample_data
ENV MRR_MCP_DATA=/app/sample_data
EXPOSE 8097
CMD ["python3", "pricing_mcp.py", "--host", "0.0.0.0", "--port", "8097"]
