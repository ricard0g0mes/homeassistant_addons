ARG BUILD_FROM
FROM $BUILD_FROM

ENV LANG C.UTF-8
COPY app /app
WORKDIR /app
RUN pip3 install --no-cache-dir -r requirements.txt
COPY run.sh /
RUN chmod a+x /run.sh
CMD ["/run.sh"]
