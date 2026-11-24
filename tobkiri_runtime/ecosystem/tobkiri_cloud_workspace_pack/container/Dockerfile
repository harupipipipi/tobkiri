# The reviewed OCI index fixes every supported platform's base image bytes.
# This recipe performs no dependency install, secret mount, shell or cloud login.
FROM python:3.13-slim@sha256:bb2988715db2cf7ace7b53f38f3cffbef7c7046a656bee66245eb0ed386e2e81
COPY container/workspace_capsule_v1.py /opt/tobkiri/capsule.py
COPY runtime/recipe.py /opt/tobkiri/recipe.py
COPY container/runtime.py /opt/tobkiri/workspace_runtime.py
USER 65532:65532
WORKDIR /workspace
EXPOSE 8765
HEALTHCHECK --interval=15s --timeout=3s --start-period=5s --retries=3 CMD ["python3", "-B", "/opt/tobkiri/workspace_runtime.py", "--healthcheck"]
ENTRYPOINT ["python3", "-B", "/opt/tobkiri/workspace_runtime.py"]
CMD ["--capsule", "/input/workspace.zip", "--workspace", "/workspace", "--listen", "0.0.0.0", "--port", "8765"]
