ARG BUILD_FROM
FROM ${BUILD_FROM}

ENV LANG C.UTF-8

# Alpine base: instalar Python (mais fiável que depender de base-python)
RUN apk add --no-cache python3 py3-pip

WORKDIR /app
COPY app/requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt
COPY app/*.py ./
COPY run.sh /run.sh
RUN chmod +x /run.sh

EXPOSE 8765
CMD ["/run.sh"]
