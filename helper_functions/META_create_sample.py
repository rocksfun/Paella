import os
import re
from datetime import datetime
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, 
    QTextEdit, QPushButton, QMessageBox, QFormLayout
)
from PySide6.QtCore import Qt
from helper_functions.META_sample_selection import _get_local_data_path
from helper_functions.UIUX_elements import create_button

class CreateSampleDialog(QDialog):
    """Dialog for creating a new sample manually."""
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Create New Sample")
        self.setMinimumWidth(500)
        self.setModal(True)
        
        self.local_data_path = _get_local_data_path()
        self.manual_index = self._get_next_manual_index()
        
        self._setup_ui()
        self._update_preview()
        
    def _setup_ui(self):
        """Set up the dialog UI."""
        layout = QVBoxLayout(self)
        layout.setSpacing(15)
        
        # Instructions
        info_label = QLabel("Create a new sample folder manually.")
        info_label.setStyleSheet("font-weight: bold; font-size: 11pt;")
        layout.addWidget(info_label)
        
        form_layout = QFormLayout()
        
        # Nickname input
        self.nickname_input = QLineEdit()
        self.nickname_input.setPlaceholderText("Enter nickname (no spaces/special chars)")
        self.nickname_input.setStyleSheet("background-color: white;")
        self.nickname_input.textChanged.connect(self._on_nickname_changed)
        form_layout.addRow("Nickname:", self.nickname_input)
        
        # Conditions input
        self.conditions_input = QTextEdit()
        self.conditions_input.setPlaceholderText("Enter conditions (one per line)")
        self.conditions_input.setMaximumHeight(100)
        self.conditions_input.setStyleSheet("background-color: white;")
        self.conditions_input.textChanged.connect(self._validate_inputs)
        form_layout.addRow("Conditions:", self.conditions_input)
        
        layout.addLayout(form_layout)
        
        # Preview section
        preview_group = QVBoxLayout()
        
        # Sample Name Preview
        preview_label = QLabel("Sample Name Preview:")
        self.preview_value = QLabel()
        self.preview_value.setStyleSheet("font-family: monospace; font-weight: bold; color: #0078d7; padding: 5px; background-color: #f0f0f0; border-radius: 3px;")
        preview_group.addWidget(preview_label)
        preview_group.addWidget(self.preview_value)
        
        # Conditions Preview
        self.conditions_preview_label = QLabel("Conditions Preview:")
        self.conditions_preview_value = QLabel()
        self.conditions_preview_value.setStyleSheet("font-family: monospace; color: #333; padding: 5px; background-color: #f9f9f9; border: 1px solid #eee; border-radius: 3px;")
        self.conditions_preview_value.setWordWrap(True)
        preview_group.addWidget(self.conditions_preview_label)
        preview_group.addWidget(self.conditions_preview_value)
        
        layout.addLayout(preview_group)
        
        # Buttons
        button_layout = QHBoxLayout()
        button_layout.addStretch()
        
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        button_layout.addWidget(cancel_btn)
        
        self.create_btn = create_button("Create Sample", "success")
        self.create_btn.clicked.connect(self.accept)
        self.create_btn.setEnabled(False)  # Disabled until valid input
        button_layout.addWidget(self.create_btn)
        
        layout.addLayout(button_layout)
        
    def _get_next_manual_index(self):
        """Get the next manual creation index from tracking file."""
        tracking_file = os.path.join(self.local_data_path, "last_manual_sample.txt")
        
        if not os.path.exists(tracking_file):
            return 1
            
        try:
            with open(tracking_file, 'r') as f:
                last_name = f.read().strip()
                
            # Extract index from name (format: ..._mc{index})
            match = re.search(r'_mc(\d+)$', last_name)
            if match:
                return int(match.group(1)) + 1
            return 1
        except Exception as e:
            print(f"Error reading manual sample index: {e}")
            return 1
            
    def _on_nickname_changed(self):
        """Handle nickname changes."""
        text = self.nickname_input.text()
        
        # Validate: allow only alphanumeric and underscores
        clean_text = re.sub(r'[^a-zA-Z0-9_]', '', text)
        
        if text != clean_text:
            self.nickname_input.setText(clean_text)
            return
            
        self._update_preview()
        self._validate_inputs()
        
    def _update_preview(self):
        """Update the sample name preview."""
        nickname = self.nickname_input.text().strip()
        if not nickname:
            self.preview_value.setText("(Enter nickname to see preview)")
            return
            
        now = datetime.now()
        date_str = now.strftime("%Y_%m_%d")
        time_str = now.strftime("%H%M")
        
        # Format: YYYY_MM_DD_HHMM_nickname_mc{index}
        self.full_sample_name = f"{date_str}_{time_str}_{nickname}_mc{self.manual_index}"
        self.preview_value.setText(self.full_sample_name)
        
    def _validate_inputs(self):
        """Enable create button only if inputs are valid."""
        nickname = self.nickname_input.text().strip()
        conditions_raw = self.conditions_input.toPlainText().strip()
        
        # Parse conditions
        conditions = self._parse_conditions(conditions_raw)
        
        self._update_conditions_preview(conditions)
        
        is_valid = bool(nickname) and len(conditions) > 0
        self.create_btn.setEnabled(is_valid)

    def _parse_conditions(self, text):
        """Parse conditions from text (supports line breaks and commas)."""
        if not text:
            return []
        # Split by newline first, then by comma for each line
        lines = text.split('\n')
        all_conditions = []
        for line in lines:
            # Split by comma and strip whitespace
            parts = [c.strip() for c in line.split(',') if c.strip()]
            all_conditions.extend(parts)
        return all_conditions

    def _update_conditions_preview(self, conditions):
        """Update the conditions preview display."""
        if not conditions:
            self.conditions_preview_value.setText("(Enter conditions to see preview)")
            return
            
        preview_lines = []
        for i, condition in enumerate(conditions, 1):
            preview_lines.append(f"Condition {i}: {condition}")
            
        self.conditions_preview_value.setText('\n'.join(preview_lines))
        
    def get_sample_data(self):
        """Return the created sample data."""
        return {
            "name": self.full_sample_name,
            "nickname": self.nickname_input.text().strip(),
            "conditions": self._parse_conditions(self.conditions_input.toPlainText().strip())
        }

class EditConditionsDialog(QDialog):
    """Dialog for editing (adding) conditions to an existing sample."""
    
    def __init__(self, current_conditions, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Edit Conditions")
        self.setMinimumWidth(400)
        self.setModal(True)
        self.current_conditions = current_conditions
        
        self._setup_ui()
        
    def _setup_ui(self):
        layout = QVBoxLayout(self)
        
        # Existing conditions (readonly)
        layout.addWidget(QLabel("Existing Conditions:"))
        existing_text = QTextEdit()
        existing_text.setReadOnly(True)
        existing_text.setPlainText("\n".join(self.current_conditions))
        existing_text.setStyleSheet("background-color: #f0f0f0; color: #555;")
        layout.addWidget(existing_text)
        
        # New conditions input
        layout.addWidget(QLabel("Add New Conditions (one per line):"))
        self.new_conditions_input = QTextEdit()
        self.new_conditions_input.setPlaceholderText("Enter new conditions here...")
        self.new_conditions_input.setStyleSheet("background-color: white;")
        layout.addWidget(self.new_conditions_input)
        
        # Buttons
        button_layout = QHBoxLayout()
        button_layout.addStretch()
        
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        button_layout.addWidget(cancel_btn)
        
        save_btn = create_button("Save Conditions", "primary")
        save_btn.clicked.connect(self.accept)
        button_layout.addWidget(save_btn)
        
        layout.addLayout(button_layout)
        
    def get_new_conditions(self):
        """Return list of newly added conditions."""
        text = self.new_conditions_input.toPlainText().strip()
        if not text:
            return []
        
        # Split by newline first, then by comma for each line
        lines = text.split('\n')
        all_conditions = []
        for line in lines:
            # Split by comma and strip whitespace
            parts = [c.strip() for c in line.split(',') if c.strip()]
            all_conditions.extend(parts)
        return all_conditions
