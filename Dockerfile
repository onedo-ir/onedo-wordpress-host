FROM gitea.tmayt.ir/thaiostream/ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

# ===== Iranian mirror =====
RUN echo "deb http://mirror.arvancloud.ir/ubuntu/ jammy main restricted universe multiverse" > /etc/apt/sources.list \
 && echo "deb http://mirror.arvancloud.ir/ubuntu/ jammy-updates main restricted universe multiverse" >> /etc/apt/sources.list \
 && echo "deb http://mirror.arvancloud.ir/ubuntu/ jammy-backports main restricted universe multiverse" >> /etc/apt/sources.list \
 && echo "deb http://mirror.arvancloud.ir/ubuntu/ jammy-security main restricted universe multiverse" >> /etc/apt/sources.list

# ===== Install all services =====
RUN apt-get update && apt-get install -y \
    apache2 mariadb-server \
    php php-mysql php-cli php-curl php-xml php-mbstring libapache2-mod-php \
    wget curl unzip tar openssl supervisor \
    && rm -rf /var/lib/apt/lists/*

# ===== Install FileBrowser =====
COPY filebrowser /filebrowser
RUN chmod +x /filebrowser/get.sh
RUN /filebrowser/get.sh

# ===== WordPress =====
COPY wordpress-6.9.4.tar.gz /tmp/latest.tar.gz
RUN cd /tmp \
 && tar -xzf latest.tar.gz \
 && mv wordpress /var/www/html

# ===== Permissions =====
RUN chown -R www-data:www-data /var/www/html

# ===== Supervisor config =====
COPY supervisord.conf /etc/supervisor/conf.d/supervisord.conf

# ===== Init script =====
COPY init.sh /init.sh
RUN chmod +x /init.sh

EXPOSE 80 8080 3306

CMD ["/usr/bin/supervisord", "-n"]