#! /bin/sh
echo "RHEL-7 RPM Deploy testing with $1 build_type...."

random_string=$(cat /dev/urandom | tr -cd 'a-z0-9' | head -c 5)
instance_name="psi${random_string}"
build_type=${1:-'released'}
platform="rhel-7"
rhbuild="4.3"
test_suite="suites/nautilus/ansible/tier_1_deploy.yaml"
test_conf="conf/nautilus/ansible/tier_1_deploy.yaml"
test_inventory="conf/inventory/rhel-7.9-server-x86_64.yaml"
return_code=0

$WORKSPACE/.venv/bin/python run.py --v2 --osp-cred $HOME/osp-cred-ci-2.yaml --rhbuild $rhbuild --platform $platform --build $build_type --instances-name $instance_name --global-conf $test_conf --suite $test_suite --inventory $test_inventory --post-results --log-level DEBUG --report-portal

if [ $? -ne 0 ]; then
  return_code=1
fi

$WORKSPACE/.venv/bin/python run.py --cleanup $instance_name --osp-cred $HOME/osp-cred-ci-2.yaml --log-level debug

if [ $? -ne 0 ]; then
  echo "cleanup instance failed for instance $instance_name"
fi

exit ${return_code}
