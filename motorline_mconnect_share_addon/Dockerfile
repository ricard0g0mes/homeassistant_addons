ARG BUILD_FROM
FROM ${BUILD_FROM}

ENV LANG=C.UTF-8

RUN apk add --no-cache python3 py3-pip

WORKDIR /app
COPY app/requirements.txt .
RUN pip3 install --no-cache-dir --break-system-packages -r requirements.txt
COPY app/*.py ./
COPY run.sh /run.sh
RUN chmod +x /run.sh

EXPOSE 8766
CMD ["/run.sh"]
