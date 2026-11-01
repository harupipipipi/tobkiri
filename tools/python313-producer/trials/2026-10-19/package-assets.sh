set -eu
/usr/bin/python3 /helper/package.py \
  --input-root /inputs \
  --manifest /manifest.json \
  --manifest-sha256 8e463d666b95dfe2cb6c8678305477867feb8a42a42366049dcea9f3f5d9e9be \
  --worker /worker \
  --worker-sha256 97d1fc4054095dd599a9fdedb1757e3f0eae743150e21289867bb666d0589b45 \
  --destination /work/assets
