import json
import time

from utility.log import Log

LOG = Log(__name__)


def validate_qos(client, device, **kw):

    bandwidth = {"mb_read/s": [], "mb_write/s": [], "mb_r/s": [], "mb_w/s": []}
    try:
        client.exec_command(cmd="dnf install -y sysstat", sudo=True, long_running=True)
        for _ in range(3):
            out, _, _, _ = client.exec_command(
                cmd="iostat -m -dx 5 1", sudo=True, verbose=True
            )

            lines = out.strip().split("\n")
            found_header = False

            for line in lines:

                # Identify the headers row
                if "Device" in line and "rMB/s" in line and "wMB/s" in line:
                    found_header = True
                    continue

                if found_header:
                    parts = line.split()
                    if len(parts) >= 6 and parts[0] == device:
                        mb_read = float(parts[2])  # MB_read/s
                        mb_write = float(parts[8])  # MB_wrtn/s
                        mb_write_iops = float(parts[7])  # MB_w/s
                        mb_read_iops = float(parts[1])  # MB_rs

                        bandwidth["mb_read/s"].append(mb_read)
                        bandwidth["mb_write/s"].append(mb_write)
                        bandwidth["mb_r/s"].append(mb_write_iops)
                        bandwidth["mb_w/s"].append(mb_read_iops)
                        break

            time.sleep(5)

        if "r-megabytes-per-second" in kw:
            limit = float(kw["r-megabytes-per-second"])
            if all(r < limit for r in bandwidth["mb_read/s"]):
                print(
                    f"QoS validated for {device}: Read values {bandwidth['mb_read/s']} "
                    f"are below {kw['r-megabytes-per-second']} MB/s."
                )
            else:
                raise Exception(
                    f"QoS validation failed for {device}: Read values {bandwidth['mb_read/s']} "
                    f"exceed {kw['r-megabytes-per-second']} MB/s at least once."
                )

        if "w-megabytes-per-second" in kw:
            limit = float(kw["w-megabytes-per-second"])

            if all(w < limit for w in bandwidth["mb_write/s"]):
                print(
                    f"QoS validated for {device}: Write values {bandwidth['mb_write/s']} "
                    f"are below {limit} MB/s."
                )
            else:
                raise Exception(
                    f"QoS validation failed for {device}: Write values {bandwidth['mb_write/s']} "
                    f"exceed {limit} MB/s at least once."
                )

        if "rw-megabytes-per-second" in kw:
            max_rw_mb = kw["rw-megabytes-per-second"]
            read_bw = bandwidth["mb_read/s"]
            write_bw = bandwidth["mb_write/s"]

            # Check if both read and write bandwidths are below the specified limit
            if all(r < max_rw_mb for r in read_bw) and all(
                w < max_rw_mb for w in write_bw
            ):
                print(
                    f"QoS validated for {device}: Read values {read_bw} and Write values {write_bw} "
                    f"are below {max_rw_mb} MB/s."
                )
            else:
                raise Exception(
                    f"QoS validation failed for {device}: At least one of the Read or Write values "
                    f"exceeds {max_rw_mb} MB/s. Read values: {read_bw}, Write values: {write_bw}."
                )

        if "rw-ios-per-second" in kw:
            max_rw_mb = kw["rw-ios-per-second"]
            total_bw = [r + w for r, w in zip(bandwidth["mb_r/s"], bandwidth["mb_w/s"])]
            if all(rw < max_rw_mb for rw in total_bw):
                print(
                    f"QoS validated for {device}: Read+Write values {total_bw} "
                    f"are below {kw['rw-ios-per-second']} MB/s."
                )
            else:
                raise Exception(
                    f"QoS validation failed for {device}: Read+Write values {total_bw} "
                    f"exceed {kw['rw-ios-per-second']} MB/s at least once."
                )

    except Exception as e:
        print(f"Error: {e}")
        raise e


def verify_qos(expected_config, nvmegwcli):
    subnqn = expected_config.pop("subsystem")
    nsid = expected_config.pop("nsid")
    _config = {
        "base_cmd_args": {"format": "json"},
        "args": {"subsystem": subnqn, "nsid": nsid},
    }
    _, namespace = nvmegwcli.namespace.list(**_config)
    namespace_data = json.loads(namespace)["namespaces"][0]

    def transform_rw_ios(value):
        quotient = value // 1000
        if value % 1000 == 0:
            return value
        transformed_quotient = quotient + 1
        return transformed_quotient * 1000

    for key, expected_value in expected_config.items():
        actual_value = namespace_data.get(
            key.replace("-", "_").replace("megabytes", "mbytes"), ""
        )
        if key == "rw-ios-per-second":
            expected_value = transform_rw_ios(expected_value)
        if int(actual_value) != int(expected_value):
            raise Exception(
                f"QoS verification failed for {key}: Expected {expected_value}, got {actual_value}"
            )

    LOG.info("Verification of QoS values is successful")
