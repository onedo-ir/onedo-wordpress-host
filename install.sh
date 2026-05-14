#!/bin/bash

# Website name
SITE_NAME="${1:-mywebsite}"

# Generate random passwords
RAND_PASS=$(openssl rand -base64 18 | tr -dc 'A-Za-z0-9' | head -c 24)
RAND_PASS_DB=$(openssl rand -base64 18 | tr -dc 'A-Za-z0-9' | head -c 24)

# Output file
COMPOSE_FILE="docker-compose.yml"

cat > "$COMPOSE_FILE" <<EOF
services:
  wordpress:
    image: gitea.tmayt.ir/thaiostream/wp
    container_name: ${SITE_NAME}
    build: .
    ports:
      - "8725:80"
      - "8726:8080"
    environment:
      FILEBROWSER_USER: admin
      FILEBROWSER_PASS: ${RAND_PASS}
      DB_NAME: ${SITE_NAME}_db
      DB_USER: ${SITE_NAME}_usr
      DB_PASS: ${RAND_PASS_DB}
      DB_ROOT_PASS: ${RAND_PASS_DB}
    volumes:
      - wp_html:/var/www/html
      - wp_db:/var/lib/mysql
      - filebrowser_db:/database
      - filebrowser_cfg:/config
    restart: always

volumes:
  wp_html:
  wp_db:
  filebrowser_db:
  filebrowser_cfg:
EOF

echo "docker-compose.yml generated successfully!"
echo
echo "Website Name : ${SITE_NAME}"
echo "FileBrowser Password : ${RAND_PASS}"
echo "Database Password    : ${RAND_PASS_DB}"