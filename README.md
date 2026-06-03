# Paella
Python based hardware control and data acquisition code for Travera SMRs.

## 1. Structure of the Codebase

The Paella codebase is organized as follows:

```
Paella/
├── main_gui.py              # Main GUI application entry point
├── pyImage.py               # Image control module
├── pyPump.py                # Syringe pump control module
├── pySMR.py                 # SMR (Suspended Microchannel Resonator) control module
├── paella_env.yml           # Conda environment configuration
├── helper_functions/        # Utility functions and helper modules
└── references/              # Configuration files and reference data
    ├── pypump_routines/     # Pump routine CSV files
    └── paella.spec          # PyInstaller packaging specification
```

The codebase follows a modular architecture where:
- **Main scripts** (`pyImage.py`, `pyPump.py`, `pySMR.py`) are standalone modules that can be run independently or integrated into the main GUI
- **Helper functions** provide reusable utilities for FPGA communication, UDP data management, image processing, SMR operations, system configuration, and data visualization
- **Configuration files** in the `references/` directory store settings, parameters and fluidic programs in directly edittable TOML or CSV files

## 2. Brief Description of the 4 Major Scripts

### main_gui.py
The main GUI application that integrates all three control components into a single tiled interface. It provides:
- A startup dialog for selecting manual control or automated setup mode
- System configuration validation (checks for system config file and chip logging status)
- A tiled layout displaying:
  - **Image Control** (top-left, 66% width, 65% height)
  - **Syringe Control** (top-right, 34% width, 65% height)
  - **SMR Control** (bottom, 100% width, 35% height)
- Automated setup sequence: initializes cameras, connects syringe pumps, and initializes all pumps

### pyImage.py
Image acquisition and processing control module for Basler cameras. Key features include:
- Multi-camera support (brightfield and fluorescent channels)
- Real-time image display with ROI (Region of Interest) overlay
- Automatic ROI detection using edge detection and line detection algorithms
- Focus calculation and tracking
- Camera configuration management
- Integration with NI-DAQmx for hardware triggering (optional)
- ROI-based analysis with histogram, angle, and focus plots
- Supports both standalone operation and embedding in other applications

### pyPump.py
Syringe pump control module for managing multiple syringe pumps via serial communication. Features include:
- Serial communication with pumps using a dedicated thread to prevent GUI blocking
- Support for multiple pumps (addresses 0-9) with individual control
- Visual syringe fill level indicators with animation
- Valve control for multi-port pumps
- Routine execution system for automated sequences (CSV-based)
- Real-time status monitoring and error handling
- Pump initialization and homing sequences
- Volume and speed control with step-based positioning
- Configuration file management for pump settings

### pySMR.py
SMR (Suspended Microchannel Resonator) control module for FPGA-based frequency measurement. Capabilities include:
- TCP communication with FPGA for settings and command control
- Real-time frequency data acquisition via UDP multicast
- High-performance data reception using multiprocessing or threading
- Real-time frequency vs. time plotting with pyqtgraph
- FPGA parameter configuration and register value calculation
- Quick controls for Run status, PLL delay, and PLL drive amplitude
- Diagnostic plotting for timestamp and packet rate analysis
- Data storage with automatic file management

## 3. Helper Functions Modules

The `helper_functions/` package contains utility modules organized by functionality:

### FPGA Communication
- **FPGA_connect.py**: Functions for establishing TCP connections to FPGA devices with timeout and error handling
- **FPGA_tcp_manager.py**: Thread-safe command queue system for sending commands to the FPGA via TCP using a Producer-Consumer pattern with QThread-based worker
- **FPGA_UserParametersToRegisterValues.py**: GUI and functions for converting user parameters to FPGA register values, emulating LabVIEW Formula node behavior with proper rounding

### UDP Data Management
- **UDP_data_manager.py**: Centralized, thread-safe UDP data reception system that receives packets from a single UDP multicast socket and distributes them to multiple subscribers using a Producer-Consumer pattern
- **UDP_receive_data.py**: Functions for receiving data from UDP multicast connections and parsing it into frequency data. Includes standalone GUI components for testing/debugging

### Image Processing
- **ROI_detection.py**: Functions for detecting ROI boundaries using edge detection, supporting both axis-aligned edge detection and derivative-based corner detection for angle-tolerant line detection
- **ROI_focus.py**: Functions for measuring image focus/sharpness using the variance of Laplacian method
- **ROI_plots.py**: Plot widgets for ROI/Alignment tab including histogram, angle, and focus plots, optimized for performance with OpenGL acceleration

### SMR Operations
- **SMR_settings_io.py**: Functions for reading and writing SMR settings to CSV files with metadata and FPGA user parameters
- **SMR_sweep_frequencies.py**: Module for initializing SMR connections and generating FPGA parameter strings for frequency sweeps
- **SMR_set_delays.py**: Module for setting SMR delays and bias voltages, including DAQ-based bias control

### System & Configuration
- **SYSTEM_pull_config_io.py**: Functions for reading and parsing the system configuration file (TOML-style)
- **UIUX_elements.py**: Common UI components and styling utilities used across the application

### Metadata & Sample Management
- **META_sample_selection.py**: Functions for sample selection, local data path management, and data copying
- **META_create_sample.py**: Dialogs for manual sample creation and condition editing

### Data Analysis & Visualization
- **frequency_plot.py**: Helper functions for visualizing frequency data from SMR packets
- **diagnostic_plot.py**: Helper functions for visualizing packet timestamp diagnostics
- **DATA_frequency_analysis.py**: Advanced frequency data analysis and processing utilities
- **DATA_save_udp.py**: Logic for saving raw UDP streams to disk

### Maintenance & Utilities
- **FPGA_relay_reset.py**: Sequence for resetting the FPGA relay via DAQ digital output
- **PUMP_kickback.py**: Utility for handling pump kickback compensation
- **package_paella.py**: Automation script for packaging the repository into a standalone executable

## 4. Overview of Core Packages Used

The project relies on the following core packages (as specified in `paella_env.yml`):

### GUI Framework
- **PySide6** (6.10.1): Qt-based GUI framework for building the user interface
- **pyqtgraph** (0.14.0): High-performance plotting library for real-time data visualization

### Hardware Interfaces
- **pypylon** (4.2.0): Basler camera SDK wrapper for image acquisition
- **pyserial** (3.5): Serial communication library for syringe pump control
- **nidaqmx** (1.3.0): National Instruments DAQmx interface for hardware triggering (optional)

### Scientific Computing
- **numpy** (2.2.6): Fundamental package for numerical computing and array operations
- **opencv-python** (4.12.0.88): Computer vision library for image processing and ROI detection
- **matplotlib** (3.10.8): Plotting library for static and interactive visualizations

### Utilities
- **python-decouple** (3.8): Configuration management
- **requests** (2.32.5): HTTP library for web requests (if needed)
- **hightime** (1.0.0): High-resolution timing utilities

### Standard Library
The codebase also extensively uses Python's standard library modules:
- `threading` and `multiprocessing` for concurrent operations
- `socket` for network communication (TCP/UDP)
- `csv` for routine file parsing
- `struct` for binary data parsing
- `time` and `queue` for timing and inter-thread communication

## 5. Packaging for Portability

Paella can be packaged into a standalone Windows executable to run on systems without a pre-configured Python environment.

### Prerequisites
- Windows OS
- Conda environment activated (`conda activate paella`)
- PyInstaller installed (`pip install pyinstaller`)

### Packaging Steps
1. Navigate to the `helper_functions/` directory
2. Run the packaging script:
   ```bash
   python package_paella.py
   ```
3. The standalone executable will be generated in the `dist/` folder at the project root.

> [!IMPORTANT]
> The executable requires `C:/Paella local/system_config.txt` to be present on the host machine for system-specific settings. Generic reference files are bundled within the executable.
