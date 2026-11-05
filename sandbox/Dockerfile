# Sandbox image for AutoGrader: everything a C submission is ever run with.
# The image has NO network access at run time (docker run --network none).
FROM debian:bookworm-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends gcc libc6-dev valgrind cppcheck procps coreutils util-linux \
 && rm -rf /var/lib/apt/lists/*
RUN useradd -m -u 10001 runner
USER runner
WORKDIR /work
CMD ["sleep", "infinity"]
