# 基于 fuguoai 预构建基础镜像（已含全部 Python 依赖），仅覆盖后端代码
FROM fuguoai-server-base:server-base

COPY . .

# 容器级健康检查：复用 /metrics 探针（与进程内看门狗 05de1e7 互补）。
# 进程内看门狗覆盖"事件循环冻死→os._exit"；本 HEALTHCHECK 覆盖其盲区：
# 进程存活但 HTTP/事件循环不响应 /metrics（如看门狗线程本身卡住、或死锁只堵住 HTTP handler）。
# 连续 3 次失败(约 90s)才判定不健康，避免部署/高负载瞬断误杀；start-period 留给模型加载冷启。
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8002/metrics', timeout=5); sys.exit(0)"

CMD ["python", "app.py"]
