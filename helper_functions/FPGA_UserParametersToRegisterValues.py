"""
FPGA User Parameters to Register Values Module.

This module provides a GUI for converting user parameters to FPGA register values.
It emulates the behavior of a LabVIEW Formula node.
Note: Values are rounded prior to int casting to emulate Labview's Int casting behavior.
"""

import sys
import math
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QCheckBox, QComboBox, QFormLayout, QGroupBox,
    QScrollArea, QDoubleSpinBox, QSpinBox
)
from PySide6.QtCore import Qt


class ScientificDoubleSpinBox(QDoubleSpinBox):
    """QDoubleSpinBox that displays values in scientific notation with 3 decimal places."""
    
    def textFromValue(self, value):
        """Convert value to scientific notation string with 3 decimal places."""
        if value == 0:
            return "0.000e+00"
        return f"{value:.3e}"
    
    def valueFromText(self, text):
        """Convert scientific notation string to value."""
        try:
            return float(text)
        except ValueError:
            return 0.0


def calculate_register_values(
    Run=False, Enable_AGC=True, Send_data_to_pc=True,
    Run_NCO_at_fixed_freq=False, Impulse=False, Input_source=False,
    Signal_of_interest=0, DAC_A_output=0, DAC_B_output=0,
    PLL_datarate_decimation=0, Frequency=1000000, Minimum_frequency=999000,
    Maximum_frequency=10010000, CIC_rate=32767, CIC_bit_shift=16, PLL_delay=0.0,
    PLL_drive_amplitude=0.1, Feedback_delay=0, Feedback_gain=0.1,
    Resonator_Q=0.0, Loop_bandwidth=10000000, Loop_order=1
):
    """
    Calculate FPGA register values from user parameters.
    
    Returns a dictionary of all register values in U32 format.
    """
    # Calculate smr_driver_mode
    smr_driver_mode = (
        (1 if Run else 0) +
        2 * (1 if Enable_AGC else 0) +
        2**2 * (1 if Send_data_to_pc else 0) +
        2**3 * (1 if Run_NCO_at_fixed_freq else 0) +
        2**5 * (1 if Impulse else 0) +
        2**6 * (1 if Input_source else 0) +
        2**8 * Signal_of_interest +
        2**19 * DAC_A_output +
        2**22 * DAC_B_output +
        2**25 * PLL_datarate_decimation
    ) & 0xFFFFFFFF
    
    # Calculate phase increments
    phase_increment_upon_reset = int(round(Frequency / 12.5e6 * 2**32)) & 0xFFFFFFFF
    phase_increment_minimum = int(round(Minimum_frequency / 12.5e6 * 2**32)) & 0xFFFFFFFF
    phase_increment_maximum = int(round(Maximum_frequency / 12.5e6 * 2**32)) & 0xFFFFFFFF
    
    # Calculate decimator control
    decimator_control = int(round(CIC_rate + CIC_bit_shift * 2**16)) & 0xFFFFFFFF
    
    # Calculate delay and gain values
    delay = int(round(PLL_delay * (2**32 - 1))) & 0xFFFFFFFF
    nco_gain = int(round(PLL_drive_amplitude * (2**32 - 1))) & 0xFFFFFFFF
    feedback_delay_reg = int(round(Feedback_delay * 2**16)) & 0xFFFFFFFF
    feedback_gain_reg = int(round(Feedback_gain * 2**24)) & 0xFFFFFFFF
    
    # Constants
    Ts = 1e-8
    tau = Resonator_Q / (Frequency * math.pi) if Frequency > 0 else 0
    beta = 2 * math.pi * Loop_bandwidth
    gainCompensation = 2.0 / 0.89 * 8 / (2 * math.pi)
    
    # Initialize filter coefficients
    gain_proportional = 0
    gain_integral = 0
    sos_filter_0_b0 = 0
    sos_filter_0_b1 = 0
    sos_filter_0_b2 = 0
    sos_filter_0_a1 = 0
    sos_filter_0_a2 = 0
    sos_filter_1_b0 = 0
    sos_filter_1_b1 = 0
    sos_filter_1_b2 = 0
    sos_filter_1_a1 = 0
    sos_filter_1_a2 = 0
    
    # Calculate based on Loop_order
    if Loop_order == 1:
        gain_proportional = int(round(beta * Ts * gainCompensation * 2**32)) & 0xFFFFFFFF
        gain_integral = int(round(beta * Ts**2 * CIC_rate / tau * gainCompensation * 2**8 * 2**32)) & 0xFFFFFFFF if tau > 0 else 0
        sos_filter_0_b0 = int(round(1 * 2**30)) & 0xFFFFFFFF
        sos_filter_0_b1 = 0
        sos_filter_0_b2 = 0
        sos_filter_0_a1 = 0
        sos_filter_0_a2 = 0
        sos_filter_1_b0 = int(round(1 * 2**30)) & 0xFFFFFFFF
        sos_filter_1_b1 = 0
        sos_filter_1_b2 = 0
        sos_filter_1_a1 = 0
        sos_filter_1_a2 = 0
        
    elif Loop_order == 2:
        gain_proportional = int(round(1.412136/2 * beta * Ts * gainCompensation * 2**32)) & 0xFFFFFFFF
        gain_integral = int(round(1.412136/2 * beta * Ts**2 * CIC_rate / tau * gainCompensation * 2**8 * 2**32)) & 0xFFFFFFFF if tau > 0 else 0
        denominator = Ts * CIC_rate + 1.412136/2/beta if beta > 0 else 1
        sos_filter_0_b0 = int(round(Ts * CIC_rate / denominator * 2**30)) & 0xFFFFFFFF
        sos_filter_0_b1 = 0
        sos_filter_0_b2 = 0
        sos_filter_0_a1 = int(round(-1.412136/2/beta / denominator * 2**30)) & 0xFFFFFFFF if beta > 0 else 0
        sos_filter_0_a2 = 0
        sos_filter_1_b0 = int(round(1 * 2**30)) & 0xFFFFFFFF
        sos_filter_1_b1 = 0
        sos_filter_1_b2 = 0
        sos_filter_1_a1 = 0
        sos_filter_1_a2 = 0
        
    elif Loop_order == 3:
        gain_proportional = int(round(0.5 * beta * Ts * gainCompensation * 2**32)) & 0xFFFFFFFF
        gain_integral = int(round(0.5 * beta * Ts**2 * CIC_rate / tau * gainCompensation * 2**8 * 2**32)) & 0xFFFFFFFF if tau > 0 else 0
        
        gamma = math.tan(1.412136 * beta * Ts * CIC_rate / 2) if beta > 0 and CIC_rate > 0 else 0
        a0 = (gamma**2 + gamma * 1.412136 + 1) if gamma != 0 else 1
        
        sos_filter_0_b0 = int(round(gamma**2 / a0 * 2**30)) & 0xFFFFFFFF if a0 > 0 else 0
        sos_filter_0_b1 = int(round(2 * sos_filter_0_b0)) & 0xFFFFFFFF
        sos_filter_0_b2 = sos_filter_0_b0
        sos_filter_0_a1 = int(round(2 * (gamma**2 - 1) / a0 * 2**30)) & 0xFFFFFFFF if a0 > 0 else 0
        sos_filter_0_a2 = int(round((gamma**2 - gamma * 1.412136 + 1) / a0 * 2**30)) & 0xFFFFFFFF if a0 > 0 else 0
        sos_filter_1_b0 = int(round(1 * 2**30)) & 0xFFFFFFFF
        sos_filter_1_b1 = 0
        sos_filter_1_b2 = 0
        sos_filter_1_a1 = 0
        sos_filter_1_a2 = 0
        
    elif Loop_order == 4:
        gain_proportional = int(round(0.3827 * beta * Ts * gainCompensation * 2**32)) & 0xFFFFFFFF
        gain_integral = int(round(0.3827 * beta * Ts**2 * CIC_rate / tau * gainCompensation * 2**8 * 2**32)) & 0xFFFFFFFF if tau > 0 else 0
        
        gamma = math.tan(1.3206/2 * beta * Ts * CIC_rate) if beta > 0 and CIC_rate > 0 else 0
        a0 = (gamma**2 + gamma/1.1846 + 1) if gamma != 0 else 1
        
        denominator = Ts * CIC_rate + 0.6673/beta if beta > 0 else 1
        sos_filter_0_b0 = int(round(Ts * CIC_rate / denominator * 2**30)) & 0xFFFFFFFF
        sos_filter_0_b1 = 0
        sos_filter_0_b2 = 0
        sos_filter_0_a1 = int(round(-0.6673/beta / denominator * 2**30)) & 0xFFFFFFFF if beta > 0 else 0
        sos_filter_0_a2 = 0
        sos_filter_1_b0 = int(round(gamma**2 / a0 * 2**30)) & 0xFFFFFFFF if a0 > 0 else 0
        sos_filter_1_b1 = int(round(2 * sos_filter_1_b0)) & 0xFFFFFFFF
        sos_filter_1_b2 = sos_filter_1_b0
        sos_filter_1_a1 = int(round(2 * (gamma**2 - 1) / a0 * 2**30)) & 0xFFFFFFFF if a0 > 0 else 0
        sos_filter_1_a2 = int(round((gamma**2 - gamma/1.1846 + 1) / a0 * 2**30)) & 0xFFFFFFFF if a0 > 0 else 0
        
    elif Loop_order == 5:
        gain_proportional = int(round(0.3090 * beta * Ts * gainCompensation * 2**32)) & 0xFFFFFFFF
        gain_integral = int(round(0.3090 * beta * Ts**2 * CIC_rate / tau * gainCompensation * 2**8 * 2**32)) & 0xFFFFFFFF if tau > 0 else 0
        
        gamma0 = math.tan(1.2223 * beta * Ts * CIC_rate / 2) if beta > 0 and CIC_rate > 0 else 0
        a0_0 = (gamma0**2 + gamma0/2.0467 + 1) if gamma0 != 0 else 1
        gamma1 = math.tan(1.4717 * beta * Ts * CIC_rate / 2) if beta > 0 and CIC_rate > 0 else 0
        a0_1 = (gamma1**2 + gamma1/0.5577 + 1) if gamma1 != 0 else 1
        
        sos_filter_0_b0 = int(round(gamma0**2 / a0_0 * 2**30)) & 0xFFFFFFFF if a0_0 > 0 else 0
        sos_filter_0_b1 = int(round(2 * sos_filter_0_b0)) & 0xFFFFFFFF
        sos_filter_0_b2 = sos_filter_0_b0
        sos_filter_0_a1 = int(round(2 * (gamma0**2 - 1) / a0_0 * 2**30)) & 0xFFFFFFFF if a0_0 > 0 else 0
        sos_filter_0_a2 = int(round((gamma0**2 - gamma0/2.0467 + 1) / a0_0 * 2**30)) & 0xFFFFFFFF if a0_0 > 0 else 0
        
        sos_filter_1_b0 = int(round(gamma1**2 / a0_1 * 2**30)) & 0xFFFFFFFF if a0_1 > 0 else 0
        sos_filter_1_b1 = int(round(2 * sos_filter_1_b0)) & 0xFFFFFFFF
        sos_filter_1_b2 = sos_filter_1_b0
        sos_filter_1_a1 = int(round(2 * (gamma1**2 - 1) / a0_1 * 2**30)) & 0xFFFFFFFF if a0_1 > 0 else 0
        sos_filter_1_a2 = int(round((gamma1**2 - gamma1/0.5577 + 1) / a0_1 * 2**30)) & 0xFFFFFFFF if a0_1 > 0 else 0
    
    return {
        'smr_driver_mode': smr_driver_mode,
        'phase_increment_upon_reset': phase_increment_upon_reset,
        'phase_increment_minimum': phase_increment_minimum,
        'phase_increment_maximum': phase_increment_maximum,
        'decimator_control': decimator_control,
        'delay': delay,
        'nco_gain': nco_gain,
        'feedback_delay': feedback_delay_reg,
        'feedback_gain': feedback_gain_reg,
        'gain_proportional': gain_proportional,
        'gain_integral': gain_integral,
        'sos_filter_0_b0': sos_filter_0_b0,
        'sos_filter_0_b1': sos_filter_0_b1,
        'sos_filter_0_b2': sos_filter_0_b2,
        'sos_filter_0_a1': sos_filter_0_a1,
        'sos_filter_0_a2': sos_filter_0_a2,
        'sos_filter_1_b0': sos_filter_1_b0,
        'sos_filter_1_b1': sos_filter_1_b1,
        'sos_filter_1_b2': sos_filter_1_b2,
        'sos_filter_1_a1': sos_filter_1_a1,
        'sos_filter_1_a2': sos_filter_1_a2,
    }


class FPGAParameterWidget(QWidget):
    """Widget for FPGA parameter input and register value display."""
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setup_ui()
        self.update_values()
    
    def setup_ui(self):
        """Set up the user interface."""
        main_layout = QHBoxLayout(self)
        main_layout.setContentsMargins(10, 10, 10, 10)
        main_layout.setSpacing(10)
        
        # Left column: Input parameters
        input_scroll = QScrollArea()
        input_scroll.setWidgetResizable(True)
        input_scroll.setMaximumWidth(300)
        input_widget = QWidget()
        input_layout = QVBoxLayout(input_widget)
        
        # Create all input widgets first
        self.smr_driver_id = QSpinBox()
        self.smr_driver_id.setRange(0, 1000)
        self.smr_driver_id.setValue(0)
        self.run_check = QCheckBox()
        self.send_data_to_pc_check = QCheckBox()
        self.send_data_to_pc_check.setChecked(True)
        self.run_nco_at_fixed_freq_check = QCheckBox()
        self.input_source_combo = QComboBox()
        self.input_source_combo.addItems(['channel_a', 'channel_b'])
        self.input_source_combo.setCurrentIndex(0)
        self.dac_a_output_combo = QComboBox()
        self.dac_a_output_combo.addItems(['off', 'PLL NCO', 'Feedback', 'Feedthrough', 'Mixed data'])
        self.dac_a_output_combo.setCurrentIndex(0)
        self.dac_b_output_combo = QComboBox()
        self.dac_b_output_combo.addItems(['off', 'PLL NCO', 'Feedback', 'Feedthrough', 'Mixed data'])
        self.dac_b_output_combo.setCurrentIndex(0)
        self.signal_of_interest_combo = QComboBox()
        self.signal_of_interest_combo.addItems(['PLL Frequency', 'error signal', 'magnitude', 'agc error signal', 'mixdown'])
        self.signal_of_interest_combo.setCurrentIndex(0)
        self.frequency = ScientificDoubleSpinBox()
        self.frequency.setRange(0.0, 1e7)
        self.frequency.setValue(1000000.0)
        self.frequency.setDecimals(3)
        self.cic_rate = QSpinBox()
        self.cic_rate.setRange(0, 100000)
        self.cic_rate.setValue(32767)
        self.cic_bit_shift = QSpinBox()
        self.cic_bit_shift.setRange(0, 100)
        self.cic_bit_shift.setValue(16)
        
        self.feedback_delay = QSpinBox()
        self.feedback_delay.setRange(0, 100000)
        self.feedback_delay.setValue(0)
        self.feedback_gain = QDoubleSpinBox()
        self.feedback_gain.setRange(0.0, 1e9)
        self.feedback_gain.setValue(0.1)
        self.feedback_gain.setDecimals(0)
        
        self.pll_datarate_decimation = QComboBox()
        self.pll_datarate_decimation.addItems(['1', '2', '4', '8', '16', '32'])
        self.pll_datarate_decimation.setCurrentIndex(0)
        self.minimum_frequency = ScientificDoubleSpinBox()
        self.minimum_frequency.setRange(0.0, 1e7)
        self.minimum_frequency.setValue(999000.0)
        self.minimum_frequency.setDecimals(3)
        self.maximum_frequency = ScientificDoubleSpinBox()
        self.maximum_frequency.setRange(0.0, 1e7)
        self.maximum_frequency.setValue(1001000.0)
        self.maximum_frequency.setDecimals(3)
        self.resonator_q = QDoubleSpinBox()
        self.resonator_q.setRange(0.0, 1e9)
        self.resonator_q.setValue(0.0)
        self.resonator_q.setDecimals(0)
        self.loop_order = QSpinBox()
        self.loop_order.setRange(1, 5)
        self.loop_order.setValue(1)
        self.loop_bandwidth = QDoubleSpinBox()
        self.loop_bandwidth.setRange(0.0, 1e9)
        self.loop_bandwidth.setValue(10000000.0)
        self.loop_bandwidth.setDecimals(0)
        self.pll_delay = QDoubleSpinBox()
        self.pll_delay.setRange(0.0, 1.0)
        self.pll_delay.setValue(0.0)
        self.pll_delay.setDecimals(3)
        self.pll_delay.setSingleStep(0.01)
        self.pll_drive_amplitude = QDoubleSpinBox()
        self.pll_drive_amplitude.setRange(0.0, 1.0)
        self.pll_drive_amplitude.setValue(0.1)
        self.pll_drive_amplitude.setDecimals(3)
        self.pll_drive_amplitude.setSingleStep(0.01)
        
        # Hidden widgets for parameters not in the new sections
        self.enable_agc_check = QCheckBox()
        self.enable_agc_check.setChecked(True)
        self.impulse_check = QCheckBox()
        
        # Main section
        main_group = QGroupBox("Main")
        main_group_layout = QFormLayout()
        main_group_layout.addRow("SmrDriverId:", self.smr_driver_id)
        main_group_layout.addRow("Run:", self.run_check)
        main_group_layout.addRow("Send data to PC:", self.send_data_to_pc_check)
        main_group_layout.addRow("Run NCO at fixed freq:", self.run_nco_at_fixed_freq_check)
        main_group_layout.addRow("Input source:", self.input_source_combo)
        main_group_layout.addRow("DAC A output:", self.dac_a_output_combo)
        main_group_layout.addRow("DAC B output:", self.dac_b_output_combo)
        main_group_layout.addRow("Signal of interest:", self.signal_of_interest_combo)
        main_group_layout.addRow("Frequency:", self.frequency)
        main_group_layout.addRow("CIC rate:", self.cic_rate)
        main_group_layout.addRow("CIC bit shift:", self.cic_bit_shift)
        main_group.setLayout(main_group_layout)
        input_layout.addWidget(main_group)
        
        # Feedback section
        feedback_group = QGroupBox("Feedback")
        feedback_layout = QFormLayout()
        feedback_layout.addRow("Feedback delay:", self.feedback_delay)
        feedback_layout.addRow("Feedback gain:", self.feedback_gain)
        feedback_group.setLayout(feedback_layout)
        input_layout.addWidget(feedback_group)
        
        # PLL section
        pll_group = QGroupBox("PLL")
        pll_layout = QFormLayout()
        pll_layout.addRow("PLL datarate decimation:", self.pll_datarate_decimation)
        pll_layout.addRow("Minimum frequency:", self.minimum_frequency)
        pll_layout.addRow("Maximum frequency:", self.maximum_frequency)
        pll_layout.addRow("Resonator Q:", self.resonator_q)
        pll_layout.addRow("Loop order:", self.loop_order)
        pll_layout.addRow("Loop bandwidth:", self.loop_bandwidth)
        pll_layout.addRow("PLL delay:", self.pll_delay)
        pll_layout.addRow("PLL drive amplitude:", self.pll_drive_amplitude)
        pll_layout.addRow("Impulse:", self.impulse_check)
        pll_layout.addRow("Enable AGC:", self.enable_agc_check)
        pll_group.setLayout(pll_layout)
        input_layout.addWidget(pll_group)
        
        input_layout.addStretch()
        input_scroll.setWidget(input_widget)
        main_layout.addWidget(input_scroll)
        
        # Connect all inputs to update function
        for widget in [
            self.smr_driver_id, self.run_check, self.enable_agc_check, self.send_data_to_pc_check,
            self.run_nco_at_fixed_freq_check, self.impulse_check,
            self.frequency, self.minimum_frequency,
            self.maximum_frequency, self.cic_rate, self.cic_bit_shift,
            self.pll_delay, self.pll_drive_amplitude, self.feedback_delay,
            self.feedback_gain, self.resonator_q, self.loop_bandwidth, self.loop_order
        ]:
            if isinstance(widget, QCheckBox):
                widget.toggled.connect(self.update_values)
            else:
                widget.valueChanged.connect(self.update_values)
        
        # Connect comboboxes separately
        self.input_source_combo.currentIndexChanged.connect(self.update_values)
        self.signal_of_interest_combo.currentIndexChanged.connect(self.update_values)
        self.dac_a_output_combo.currentIndexChanged.connect(self.update_values)
        self.dac_b_output_combo.currentIndexChanged.connect(self.update_values)
        self.pll_datarate_decimation.currentIndexChanged.connect(self.update_values)
        
        # Middle column: Register Values
        register_scroll = QScrollArea()
        register_scroll.setWidgetResizable(True)
        register_scroll.setMaximumWidth(300)
        register_widget = QWidget()
        register_layout = QVBoxLayout(register_widget)
        
        register_title = QLabel("Register Values (U32 Format)")
        register_title.setStyleSheet("font-size: 14pt; font-weight: bold; padding: 10px;")
        register_layout.addWidget(register_title)
        
        self.output_display = QFormLayout()
        
        # Create labels for all output values
        self.output_labels = {}
        output_names = [
            'smr_driver_mode', 'phase_increment_upon_reset', 'phase_increment_minimum',
            'phase_increment_maximum', 'decimator_control', 'gain_proportional', 'gain_integral',
            'delay', 'nco_gain', 'feedback_delay', 'feedback_gain',
            'sos_filter_0_b0', 'sos_filter_0_b1', 'sos_filter_0_b2', 'sos_filter_0_a1',
            'sos_filter_0_a2', 'sos_filter_1_b0', 'sos_filter_1_b1', 'sos_filter_1_b2',
            'sos_filter_1_a1', 'sos_filter_1_a2'
        ]
        
        for name in output_names:
            label = QLabel("0")
            label.setStyleSheet("""
                font-family: 'Courier New', monospace;
                font-size: 10pt;
                padding: 3px;
                background-color: #f9f9f9;
                border: 1px solid #ddd;
                border-radius: 3px;
            """)
            self.output_labels[name] = label
            display_name = name.replace('_', ' ').title()
            self.output_display.addRow(f"{display_name}:", label)
        
        register_layout.addLayout(self.output_display)
        register_layout.addStretch()
        register_scroll.setWidget(register_widget)
        main_layout.addWidget(register_scroll)
        
        # Right column: SetAllParametersString
        set_all_scroll = QScrollArea()
        set_all_scroll.setWidgetResizable(True)
        set_all_scroll.setMaximumWidth(300)
        set_all_widget = QWidget()
        set_all_layout = QVBoxLayout(set_all_widget)
        
        set_all_title = QLabel("SetAllParametersString")
        set_all_title.setStyleSheet("font-size: 14pt; font-weight: bold; padding: 10px;")
        set_all_layout.addWidget(set_all_title)
        
        self.set_all_parameters_label = QLabel("")
        self.set_all_parameters_label.setStyleSheet("""
            font-family: 'Courier New', monospace;
            font-size: 9pt;
            padding: 5px;
            background-color: #f9f9f9;
            border: 1px solid #ddd;
            border-radius: 3px;
        """)
        self.set_all_parameters_label.setWordWrap(True)
        self.set_all_parameters_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        set_all_layout.addWidget(self.set_all_parameters_label)
        
        set_all_layout.addStretch()
        set_all_scroll.setWidget(set_all_widget)
        main_layout.addWidget(set_all_scroll)
        
        self._setup_styles()
    
    def _generate_set_all_parameters_string(self, register_values):
        """Generate SetAllParametersString from register values."""
        smr_driver_id = self.smr_driver_id.value()
        smr_driver_id_offset = smr_driver_id * (2 ** 8)
        
        # Setting constants in order corresponding to register values
        setting_constants = [0, 3, 1, 2, 14, 17, 18, 5, 19, 6, 20, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31]
        
        # Register names in order
        output_names = [
            'smr_driver_mode', 'phase_increment_upon_reset', 'phase_increment_minimum',
            'phase_increment_maximum', 'decimator_control', 'gain_proportional', 'gain_integral',
            'delay', 'nco_gain', 'feedback_delay', 'feedback_gain',
            'sos_filter_0_b0', 'sos_filter_0_b1', 'sos_filter_0_b2', 'sos_filter_0_a1',
            'sos_filter_0_a2', 'sos_filter_1_b0', 'sos_filter_1_b1', 'sos_filter_1_b2',
            'sos_filter_1_a1', 'sos_filter_1_a2'
        ]
        
        # Generate string for each register
        result_lines = []
        for name, setting_constant in zip(output_names, setting_constants):
            register_id = smr_driver_id_offset + setting_constant
            register_value = register_values.get(name, 0)
            result_lines.append(f"Pw{register_id},{register_value}")
        
        return "\n".join(result_lines)
    
    def update_values(self):
        """Update all register values based on current inputs."""
        # Get all input values
        values = calculate_register_values(
            Run=self.run_check.isChecked(),
            Enable_AGC=self.enable_agc_check.isChecked(),
            Send_data_to_pc=self.send_data_to_pc_check.isChecked(),
            Run_NCO_at_fixed_freq=self.run_nco_at_fixed_freq_check.isChecked(),
            Impulse=self.impulse_check.isChecked(),
            Input_source=self.input_source_combo.currentIndex(),
            Signal_of_interest=self.signal_of_interest_combo.currentIndex(),
            DAC_A_output=self.dac_a_output_combo.currentIndex(),
            DAC_B_output=self.dac_b_output_combo.currentIndex(),
            PLL_datarate_decimation=self.pll_datarate_decimation.currentIndex(),
            Frequency=self.frequency.value(),
            Minimum_frequency=self.minimum_frequency.value(),
            Maximum_frequency=self.maximum_frequency.value(),
            CIC_rate=self.cic_rate.value(),
            CIC_bit_shift=self.cic_bit_shift.value(),
            PLL_delay=self.pll_delay.value(),
            PLL_drive_amplitude=self.pll_drive_amplitude.value(),
            Feedback_delay=self.feedback_delay.value(),
            Feedback_gain=self.feedback_gain.value(),
            Resonator_Q=self.resonator_q.value(),
            Loop_bandwidth=self.loop_bandwidth.value(),
            Loop_order=self.loop_order.value()
        )
        
        # Update all output labels
        for name, label in self.output_labels.items():
            value = values.get(name, 0)
            # Format as U32 (unsigned 32-bit integer)
            label.setText(f"{value}")
        
        # Update SetAllParametersString
        set_all_string = self._generate_set_all_parameters_string(values)
        self.set_all_parameters_label.setText(set_all_string)
    
    def _setup_styles(self):
        """Apply styles to the widget."""
        self.setStyleSheet("""
            QWidget {
                background-color: #f0f0f0;
            }
            QGroupBox {
                font-weight: bold;
                border: 2px solid #ccc;
                border-radius: 5px;
                margin-top: 10px;
                padding-top: 10px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 5px;
            }
            QLabel {
                font-family: 'Segoe UI', Arial;
            }
            QCheckBox::indicator {
                width: 18px;
                height: 18px;
                border: 2px solid #999;
                border-radius: 3px;
                background-color: white;
            }
            QCheckBox::indicator:checked {
                background-color: #0078d7;
                border: 2px solid #005a9e;
            }
            QCheckBox::indicator:checked:hover {
                background-color: #005a9e;
                border: 2px solid #004578;
            }
        """)


class MainWindow(QMainWindow):
    """Standalone window wrapper for FPGAParameterWidget."""
    
    def __init__(self):
        super().__init__()
        self.setWindowTitle("FPGA User Parameters to Register Values")
        self.setGeometry(100, 100, 950, 950)
        self.parameter_widget = FPGAParameterWidget()
        self.setCentralWidget(self.parameter_widget)


def main():
    """Main entry point for standalone execution."""
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    main()

