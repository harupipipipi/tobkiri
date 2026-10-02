# The approved Host must resolve an image reference ending @sha256:<digest>.
# This recipe performs no dependency install, secret mount, shell or cloud login.
ARG TOBKIRI_BASE_IMAGE
FROM ${TOBKIRI_BASE_IMAGE}
COPY runtime/capsule.py /opt/tobkiri/capsule.py
COPY container/runtime.py /opt/tobkiri/workspace_runtime.py
USER 65532:65532
WORKDIR /workspace
EXPOSE 8765
HEALTHCHECK --interval=15s --timeout=3s --start-period=5s --retries=3 CMD ["python3", "-B", "/opt/tobkiri/workspace_runtime.py", "--healthcheck"]
ENTRYPOINT ["python3", "-B", "/opt/tobkiri/workspace_runtime.py"]
CMD ["--capsule", "/input/workspace.zip", "--workspace", "/workspace", "--listen", "0.0.0.0", "--port", "8765"]
