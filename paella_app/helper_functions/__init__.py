"""
Helper functions package.

This package contains utility modules for hardware control, data acquisition,
and analysis. It includes:

- FPGA communication: TCP connection management, command queuing, and parameter
  conversion (FPGA_connect, FPGA_tcp_manager, FPGA_UserParametersToRegisterValues)
- UDP data management: Multicast data reception, parsing, and distribution
  (UDP_data_manager, UDP_receive_data)
- Image processing: ROI detection, focus measurement, and plotting widgets
  (ROI_detection, ROI_focus, ROI_plots)
- SMR operations: Settings I/O and frequency sweep functionality
  (SMR_settings_io, SMR_sweep_frequencies)
- System configuration: TOML-style config file parsing and system settings
  (SYSTEM_pull_config_io)
- Data visualization: Frequency plotting and diagnostic visualization
  (frequency_plot, diagnostic_plot)
"""

