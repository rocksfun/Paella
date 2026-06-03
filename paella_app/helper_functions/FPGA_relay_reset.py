"""FPGA Relay Reset helper module.

This module provides functionality to reset the FPGA relay by toggling a digital
output on the DAQ device. The reset sequence consists of setting the relay HIGH
for 400ms, then LOW for 100ms, repeated 5 times, ending with LOW.

The module can be executed directly as a script or imported and called from
other functions.
"""

import time
import sys
from typing import Optional

try:
    import nidaqmx
    NIDAQMX_AVAILABLE = True
except ImportError:
    NIDAQMX_AVAILABLE = False

from helper_functions.SYSTEM_pull_config_io import (
    load_system_config,
    get_daq_info,
)


def reset_fpga_relay(
    daq_name: Optional[str] = None,
    fpga_relay_address: Optional[str] = None,
    config_path: Optional[str] = None
) -> bool:
    """Reset FPGA relay by toggling digital output.

    This function performs a reset sequence on the FPGA relay:
    - HIGH for 400ms, LOW for 100ms (repeated 5 times)
    - Final state: LOW

    Args:
        daq_name: Optional DAQ device name (e.g., "Dev1").
                  If not provided, will be loaded from system config.
        fpga_relay_address: Optional FPGA relay channel address
                           (e.g., "port0/line3").
                           If not provided, will be loaded from system config.
        config_path: Optional path to system config file.
                     If not provided, uses default path.

    Returns:
        True if reset sequence completed successfully, False otherwise.
    """
    if not NIDAQMX_AVAILABLE:
        print("Error: nidaqmx package is not available.")
        return False

    # Load configuration if not provided
    if daq_name is None or fpga_relay_address is None:
        try:
            config = load_system_config(config_path)
            daq_info = get_daq_info(config)

            if daq_name is None:
                if "daq_name" not in daq_info:
                    print("Error: 'daq_name' not found in system config.")
                    return False
                daq_name = daq_info["daq_name"]

            if fpga_relay_address is None:
                if "fpga_relay" not in daq_info:
                    print("Error: 'fpga_relay' not found in system config.")
                    return False
                fpga_relay_address = daq_info["fpga_relay"]

        except FileNotFoundError as e:
            print(f"Error: System config file not found: {e}")
            return False
        except Exception as e:
            print(f"Error loading system config: {e}")
            return False

    # Construct full channel name
    channel_name = f"{daq_name}/{fpga_relay_address}"

    try:
        print(f"Starting FPGA relay reset sequence on {channel_name}...")

        # Perform reset sequence: HIGH for 400ms, LOW for 100ms, repeat 5 times
        with nidaqmx.Task() as task:
            task.do_channels.add_do_chan(channel_name)

            for cycle in range(5):
                # Set HIGH
                task.write(True)
                time.sleep(0.4)  # 400ms

                # Set LOW
                task.write(False)
                time.sleep(0.1)  # 100ms

                print(f"  Cycle {cycle + 1}/5 completed")

        # Ensure final state is LOW (already set in last cycle, but explicit for clarity)
        with nidaqmx.Task() as task:
            task.do_channels.add_do_chan(channel_name)
            task.write(False)

        print("FPGA relay reset sequence completed successfully.")
        return True

    except Exception as e:
        print(f"Error during FPGA relay reset: {e}")
        return False


def main():
    """Main entry point when script is executed directly."""
    success = reset_fpga_relay()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
