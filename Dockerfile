FROM nvidia/cuda:13.0.1-runtime-ubuntu24.04@sha256:c3fde347d52d578c84fd644bc177bc7ec333feaf11550d990da4084d7612e4c7

ENV DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    PATH=/opt/venv/bin:$PATH H3_CONTAINER=1 H3_ROOT=/workspace/h3 H3_DATA_DIR=/workspace/h3/app \
    H3_MEMORY_MODE=cgroup H3_TLS_MODE=selfsigned \
    OLLAMA_HOST=127.0.0.1:11434 OLLAMA_MAX_LOADED_MODELS=1 OLLAMA_NUM_PARALLEL=1 \
    OLLAMA_KEEP_ALIVE=0 OLLAMA_NO_CLOUD=1 MALLOC_ARENA_MAX=2

# Frozen Ubuntu package index; versions also recorded in the resulting image.
RUN sed -i -E 's#https?://(archive.ubuntu.com/ubuntu/?|security.ubuntu.com/ubuntu/?)#https://snapshot.ubuntu.com/ubuntu/20261012T000000Z/#g' /etc/apt/sources.list.d/ubuntu.sources \
    && apt-get -o Acquire::Check-Valid-Until=false update \
    && apt-get install -y --no-install-recommends python3 python3-venv git ca-certificates curl zstd \
       ffmpeg libgl1 libglib2.0-0 nginx apache2-utils supervisor openssh-server openssl \
    && rm -f /etc/ssh/ssh_host_* \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 --shell /bin/bash h3 \
    && python3 -m venv /opt/venv

RUN git init /opt/comfy \
    && git -C /opt/comfy remote add origin https://github.com/Comfy-Org/ComfyUI.git \
    && git -C /opt/comfy fetch --depth 1 origin 73c9bad4d21e7addbe1d13bc92eee0f1431b017d \
    && git -C /opt/comfy checkout --detach FETCH_HEAD \
    && git init /opt/comfy/custom_nodes/obvpm-timeline \
    && git -C /opt/comfy/custom_nodes/obvpm-timeline remote add origin https://github.com/chanon/comfyui-obvpm-timeline.git \
    && git -C /opt/comfy/custom_nodes/obvpm-timeline fetch --depth 1 origin 4a027a09cae607f9b36f8227a4f46ed05ccc8123 \
    && git -C /opt/comfy/custom_nodes/obvpm-timeline checkout --detach FETCH_HEAD \
    && git init /opt/comfy/custom_nodes/inspire \
    && git -C /opt/comfy/custom_nodes/inspire remote add origin https://github.com/ltdrdata/ComfyUI-Inspire-Pack.git \
    && git -C /opt/comfy/custom_nodes/inspire fetch --depth 1 origin d23db9aa544de9a6d4c609cb7005fa9e0d42031d \
    && git -C /opt/comfy/custom_nodes/inspire checkout --detach FETCH_HEAD

COPY deploy/requirements.lock /opt/h3-requirements.lock
RUN pip install --no-cache-dir --require-hashes -r /opt/h3-requirements.lock \
    && pip check \
    && dpkg-query -W > /opt/system-packages.txt

RUN curl -fL --retry 3 https://github.com/ollama/ollama/releases/download/v0.40.3/ollama-linux-amd64.tar.zst -o /tmp/ollama.tar.zst \
    && echo 'ce6f8b671a699bf1ebafb28d60dbbabf4f37b30b499145fa308a8ba72de021d6  /tmp/ollama.tar.zst' | sha256sum -c - \
    && tar --zstd -xf /tmp/ollama.tar.zst -C /usr \
    && rm /tmp/ollama.tar.zst

WORKDIR /opt/h3
COPY deploy/ollama-license.txt /usr/share/doc/ollama/LICENSE
COPY . /opt/h3
RUN cp deploy/h3-models /usr/local/bin/h3-models && chmod 755 /usr/local/bin/h3-models \
    && chown -R h3:h3 /opt/comfy
EXPOSE 8080 8081
HEALTHCHECK --interval=30s --timeout=15s --start-period=180s --retries=3 CMD ["python", "/opt/h3/deploy/healthcheck.py"]
ENTRYPOINT ["python", "/opt/h3/deploy/entrypoint.py"]
