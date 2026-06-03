"""Kickback helper module for pyPump.

This module provides functionality to execute kickback sequences on syringe pumps.
A kickback sequence dispenses and redraws a volume to prevent sample stagnation.
"""

from PySide6.QtCore import QTimer, QObject, Signal
from typing import Optional


class KickbackExecutor(QObject):
    """Executes kickback sequence with proper sequencing and waiting."""
    
    kickback_complete = Signal(bool)  # Emits True on success, False on error
    
    def __init__(self, syringe_widget, syringe_index=1):
        """
        Initialize kickback executor.
        
        Args:
            syringe_widget: SyringeControlWidget instance
            syringe_index: Syringe index (default 1 for Syringe 2)
        """
        super().__init__()
        self.syringe_widget = syringe_widget
        self.syringe_index = syringe_index
        self.step = 0
        self.kickback_volume = None
        self.kickback_rate = None
        self.error_occurred = False
        
    def execute(self, volume_ul, rate_ul_min):
        """
        Execute kickback sequence.
        
        Sequence:
        1. Interrupt syringe (send T command)
        2. Move syringe to port 3
        3. Dispense kickback_volume at kickback_rate
        4. Move syringe to port 2
        5. Draw kickback_volume at kickback_rate
        
        Args:
            volume_ul: Volume to dispense/draw in microliters
            rate_ul_min: Rate in microliters per minute
        """
        self.kickback_volume = volume_ul
        self.kickback_rate = rate_ul_min
        self.step = 0
        self.error_occurred = False
        
        # Step 1: Interrupt syringe
        self.syringe_widget.send_interrupt_command(self.syringe_index)
        # Wait a short time for interrupt to take effect
        QTimer.singleShot(200, self._step_1_complete)
    
    def _step_1_complete(self):
        """Step 1 complete: Move to port 3"""
        if self.error_occurred:
            return
        
        self.step = 1
        self.syringe_widget.move_valve(self.syringe_index, 3)
        # Wait for valve movement to complete (check pump status)
        QTimer.singleShot(500, self._wait_for_ready_step_1)
    
    def stop(self):
        """Immediately stop the kickback sequence."""
        if not self.error_occurred:
            self.error_occurred = True
            self.syringe_widget.log_text.append(f"Kickback on Syringe {self.syringe_index+1} aborted by system.")
            self.kickback_complete.emit(False)

    def _wait_for_ready_step_1(self):
        """Wait for pump to be ready after valve movement"""
        if self.error_occurred:
            return
        
        # Check if pump is ready by checking status
        # For simplicity, use a fixed delay - in production, could poll status
        # Move to step 2: Dispense
        QTimer.singleShot(500, self._step_2_complete)
    
    def _step_2_complete(self):
        """Step 2 complete: Dispense at port 3"""
        if self.error_occurred:
            return
        
        self.step = 2
        # Calculate velocity and steps
        velocity = self.syringe_widget.rate_to_velocity(str(self.kickback_rate))
        steps = self.syringe_widget.volume_to_steps(str(self.kickback_volume))
        
        if velocity is None or steps is None:
            self.error_occurred = True
            self.kickback_complete.emit(False)
            return
            
        # SAFETY CHECK: Ensure syringe has enough volume to dispense
        # Avoid 'Invalid operand' error (Error 3) by checking current plunger position
        current_pos = self.syringe_widget.current_steps[self.syringe_index]
        if current_pos < steps:
            self.syringe_widget.log_text.append(f"Kickback aborted: Syringe {self.syringe_index+1} volume too low ({current_pos} steps) for requested dispense ({steps} steps).")
            self.error_occurred = True
            self.kickback_complete.emit(False)
            return
        
        # FINAL SAFETY CHECK: One more pulse-check before sending command
        if self.error_occurred:
            return

        # Send combined command: I3V{velocity}D{steps}
        command = f"I3V{velocity}D{steps}"
        self.syringe_widget.send_command_to_pump(self.syringe_index, command)
        
        # Wait for dispense to complete
        # Estimate duration: volume / rate * 60 seconds * 1000 ms
        duration_ms = int((self.kickback_volume / self.kickback_rate) * 60 * 1000) if self.kickback_rate > 0 else 1000
        QTimer.singleShot(duration_ms + 500, self._step_3_complete)
    
    def _step_3_complete(self):
        """Step 3 complete: Move to port 2"""
        if self.error_occurred:
            return
        
        self.step = 3
        self.syringe_widget.move_valve(self.syringe_index, 2)
        # Wait for valve movement to complete
        QTimer.singleShot(500, self._wait_for_ready_step_3)
    
    def _wait_for_ready_step_3(self):
        """Wait for pump to be ready after valve movement"""
        if self.error_occurred:
            return
        
        # Move to step 4: Draw
        QTimer.singleShot(500, self._step_4_complete)
    
    def _step_4_complete(self):
        """Step 4 complete: Draw at port 2"""
        if self.error_occurred:
            return
        
        self.step = 4
        # Calculate velocity and steps
        velocity = self.syringe_widget.rate_to_velocity(str(self.kickback_rate))
        steps = self.syringe_widget.volume_to_steps(str(self.kickback_volume))
        
        if velocity is None or steps is None:
            self.error_occurred = True
            self.kickback_complete.emit(False)
            return
        
        # FINAL SAFETY CHECK: One more pulse-check before sending command
        if self.error_occurred:
            return

        # Send combined command: I2V{velocity}P{steps}
        command = f"I2V{velocity}P{steps}"
        self.syringe_widget.send_command_to_pump(self.syringe_index, command)
        
        # Wait for draw to complete
        duration_ms = int((self.kickback_volume / self.kickback_rate) * 60 * 1000) if self.kickback_rate > 0 else 1000
        QTimer.singleShot(duration_ms + 500, self._kickback_finished)
    
    def _kickback_finished(self):
        """Kickback sequence complete"""
        if not self.error_occurred:
            self.kickback_complete.emit(True)
        else:
            self.kickback_complete.emit(False)


def execute_kickback(syringe_widget, syringe_index=1, volume_ul=None, rate_ul_min=None):
    """
    Execute kickback sequence on specified syringe.
    
    Sequence:
    1. Interrupt syringe (send T command)
    2. Move syringe to port 3
    3. Dispense kickback_volume at kickback_rate
    4. Move syringe to port 2
    5. Draw kickback_volume at kickback_rate
    
    Args:
        syringe_widget: SyringeControlWidget instance
        syringe_index: Syringe index (default 1 for Syringe 2)
        volume_ul: Volume in microliters (if None, uses widget's kickback_volume_ul)
        rate_ul_min: Rate in µL/min (if None, uses widget's kickback_rate_ul_min)
    
    Returns:
        KickbackExecutor instance (can connect to kickback_complete signal)
    """
    # Use widget's values if not provided
    if volume_ul is None:
        volume_ul = syringe_widget.kickback_volume_ul
    if rate_ul_min is None:
        rate_ul_min = syringe_widget.kickback_rate_ul_min
    
    executor = KickbackExecutor(syringe_widget, syringe_index)
    executor.execute(volume_ul, rate_ul_min)
    return executor
