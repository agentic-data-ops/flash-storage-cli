echo "Pull updates"
git pull

echo "Clean installed package"
pip uninstall -y flash-storage-cli

echo "Install new package"
pip install -e .
