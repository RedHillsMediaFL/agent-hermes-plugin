ARG HERMES_REVIEW_IMAGE
FROM ${HERMES_REVIEW_IMAGE}
USER 0
RUN install -d -m 0755 /opt/review
COPY --chmod=0444 reviewer-provider-proxy.py /opt/review/provider.py
USER 993:993
ENTRYPOINT ["/opt/hermes/.venv/bin/python", "/opt/review/provider.py"]
