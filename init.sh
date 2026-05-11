#!/bin/bash

sleep 10

DB_NAME=wordpress
DB_USER=wp_user_$(openssl rand -hex 3)
DB_PASS=$(openssl rand -hex 12)
DB_ROOT_PASS=$(openssl rand -hex 16)

service mysql start || true

mysql -e "ALTER USER 'root'@'localhost' IDENTIFIED BY '${DB_ROOT_PASS}';"
mysql -uroot -p${DB_ROOT_PASS} -e "CREATE DATABASE ${DB_NAME};"
mysql -uroot -p${DB_ROOT_PASS} -e "CREATE USER '${DB_USER}'@'localhost' IDENTIFIED BY '${DB_PASS}';"
mysql -uroot -p${DB_ROOT_PASS} -e "GRANT ALL PRIVILEGES ON ${DB_NAME}.* TO '${DB_USER}'@'localhost';"
mysql -uroot -p${DB_ROOT_PASS} -e "FLUSH PRIVILEGES;"

cp /var/www/html/wp-config-sample.php /var/www/html/wp-config.php

sed -i "s/database_name_here/${DB_NAME}/" /var/www/html/wp-config.php
sed -i "s/username_here/${DB_USER}/" /var/www/html/wp-config.php
sed -i "s/password_here/${DB_PASS}/" /var/www/html/wp-config.php

chown -R www-data:www-data /var/www/html

echo "DONE"