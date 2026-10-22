# Local integration fixture only; never deployed to SecForge or AWS.
FROM ubuntu:24.04
RUN printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d && chmod 755 /usr/sbin/policy-rc.d \
    && apt-get -o Acquire::ForceIPv4=true update -qq \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends openssh-server openssh-client python3 sudo iproute2 \
    && rm -rf /var/lib/apt/lists/* && mkdir -p /run/sshd
CMD ["/usr/sbin/sshd", "-D", "-e"]
