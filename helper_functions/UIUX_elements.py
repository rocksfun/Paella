"""
UI/UX Elements Library

A centralized library of reusable UI/UX control and indicator styles
to ensure consistency across the Paella application.

All functions work with PySide6 widgets.
"""

from PySide6.QtWidgets import QPushButton, QLabel, QWidget, QFrame, QHBoxLayout, QVBoxLayout
from PySide6.QtCore import Qt
from typing import Optional, Literal


# Color Constants
class Colors:
    """Color scheme constants for UI elements."""
    
    # Primary/Blue colors
    PRIMARY_BLUE = "#0078d7"
    PRIMARY_BLUE_HOVER = "#005a9e"
    PRIMARY_BLUE_PRESSED = "#004578"
    
    BLUE = "#2196F3"
    BLUE_HOVER = "#1976D2"
    BLUE_PRESSED = "#1565C0"
    
    # Success/Green colors
    SUCCESS_GREEN = "#4CAF50"
    SUCCESS_GREEN_HOVER = "#45a049"
    SUCCESS_GREEN_PRESSED = "#3d8b40"
    
    SUCCESS_GREEN_ALT = "#28a745"
    SUCCESS_GREEN_ALT_HOVER = "#218838"
    
    # Error/Red colors
    ERROR_RED = "#f44336"
    ERROR_RED_HOVER = "#da190b"
    ERROR_RED_PRESSED = "#c62828"
    
    ERROR_RED_ALT = "#dc3545"
    ERROR_RED_ALT_HOVER = "#c82333"
    
    # Warning/Orange colors
    WARNING_ORANGE = "#FF9800"
    WARNING_ORANGE_HOVER = "#F57C00"
    WARNING_ORANGE_PRESSED = "#E65100"
    
    # LED button colors
    LED_RED_OFF = "#660000"
    LED_RED_ON = "#ff0000"
    LED_RED_BORDER_OFF = "#330000"
    LED_RED_BORDER_ON = "#cc0000"
    LED_RED_HOVER = "#770000"
    
    LED_BLUE_OFF = "#000066"
    LED_BLUE_ON = "#0000ff"
    LED_BLUE_BORDER_OFF = "#000033"
    LED_BLUE_BORDER_ON = "#0000cc"
    LED_BLUE_HOVER = "#000077"
    
    # Connection indicator colors
    CONNECTION_GREEN = "#4CAF50"
    CONNECTION_GREEN_BORDER = "#45a049"
    CONNECTION_RED = "#f44336"
    CONNECTION_RED_BORDER = "#da190b"
    CONNECTION_GRAY = "#CCCCCC"
    CONNECTION_GRAY_BORDER = "#999999"
    
    # Status colors
    STATUS_SUCCESS = "green"
    STATUS_ERROR = "red"
    STATUS_WARNING = "orange"
    STATUS_INFO = "#2196F3"
    
    # Text colors
    TEXT_DARK = "#333333"
    TEXT_GRAY = "#666666"
    TEXT_LIGHT_GRAY = "#808080"
    
    # Background colors
    BG_LIGHT = "#f0f0f0"
    BG_WHITE = "#ffffff"
    BG_DARK = "#333333"
    BG_DISABLED = "#d3d3d3"
    BG_BADGE = "#e9ecef"  # Badge style background color
    
    # Border colors
    BORDER_LIGHT = "#ccc"
    BORDER_MEDIUM = "#999"
    BORDER_DARK = "#666"
    
    # Text colors for badges
    TEXT_BADGE_LABEL = "#495057"  # Badge label text color
    TEXT_BADGE_VALUE = "#212529"  # Badge value text color


# Button Functions

def get_button_stylesheet(
    color_scheme: Literal["primary", "blue", "green", "success", "red", "error", "orange", "warning"],
    font_size: str = "12pt",
    padding: str = "10px",
    border_radius: str = "5px",
    min_width: Optional[str] = None,
    min_height: Optional[str] = None
) -> str:
    """
    Get stylesheet string for a button with specified color scheme.
    
    Args:
        color_scheme: Color scheme name
        font_size: Font size string (e.g., "12pt", "11pt")
        padding: Padding string (e.g., "10px", "8px 20px")
        border_radius: Border radius string
        min_width: Optional minimum width
        min_height: Optional minimum height
    
    Returns:
        Stylesheet string
    """
    # Define color schemes
    schemes = {
        "primary": {
            "bg": Colors.PRIMARY_BLUE,
            "hover": Colors.PRIMARY_BLUE_HOVER,
            "pressed": Colors.PRIMARY_BLUE_PRESSED
        },
        "blue": {
            "bg": Colors.BLUE,
            "hover": Colors.BLUE_HOVER,
            "pressed": Colors.BLUE_PRESSED
        },
        "green": {
            "bg": Colors.SUCCESS_GREEN,
            "hover": Colors.SUCCESS_GREEN_HOVER,
            "pressed": Colors.SUCCESS_GREEN_PRESSED
        },
        "success": {
            "bg": Colors.SUCCESS_GREEN_ALT,
            "hover": Colors.SUCCESS_GREEN_ALT_HOVER,
            "pressed": Colors.SUCCESS_GREEN_PRESSED
        },
        "red": {
            "bg": Colors.ERROR_RED,
            "hover": Colors.ERROR_RED_HOVER,
            "pressed": Colors.ERROR_RED_PRESSED
        },
        "error": {
            "bg": Colors.ERROR_RED_ALT,
            "hover": Colors.ERROR_RED_ALT_HOVER,
            "pressed": Colors.ERROR_RED_PRESSED
        },
        "orange": {
            "bg": Colors.WARNING_ORANGE,
            "hover": Colors.WARNING_ORANGE_HOVER,
            "pressed": Colors.WARNING_ORANGE_PRESSED
        },
        "warning": {
            "bg": Colors.WARNING_ORANGE,
            "hover": Colors.WARNING_ORANGE_HOVER,
            "pressed": Colors.WARNING_ORANGE_PRESSED
        }
    }
    
    scheme = schemes.get(color_scheme, schemes["primary"])
    
    min_width_str = f"min-width: {min_width};" if min_width else ""
    min_height_str = f"min-height: {min_height};" if min_height else ""
    
    return f"""
        QPushButton {{
            background-color: {scheme['bg']};
            color: white;
            font-size: {font_size};
            font-weight: bold;
            padding: {padding};
            border-radius: {border_radius};
            {min_width_str}
            {min_height_str}
        }}
        QPushButton:hover {{
            background-color: {scheme['hover']};
        }}
        QPushButton:pressed {{
            background-color: {scheme['pressed']};
        }}
        QPushButton:disabled {{
            background-color: {Colors.BG_DISABLED};
            color: {Colors.TEXT_LIGHT_GRAY};
            border: 1px solid #a0a0a0;
        }}
    """


def create_button(
    text: str,
    color_scheme: Literal["primary", "blue", "green", "success", "red", "error", "orange", "warning"] = "primary",
    font_size: str = "12pt",
    padding: str = "10px",
    border_radius: str = "5px",
    min_width: Optional[str] = None,
    min_height: Optional[str] = None,
    **kwargs
) -> QPushButton:
    """
    Create a styled QPushButton with consistent styling.
    
    Args:
        text: Button text
        color_scheme: Color scheme name
        font_size: Font size string (e.g., "12pt", "11pt")
        padding: Padding string (e.g., "10px", "8px 20px")
        border_radius: Border radius string
        min_width: Optional minimum width
        min_height: Optional minimum height
        **kwargs: Additional arguments to pass to QPushButton constructor
    
    Returns:
        Styled QPushButton instance
    """
    button = QPushButton(text, **kwargs)
    button.setStyleSheet(get_button_stylesheet(
        color_scheme=color_scheme,
        font_size=font_size,
        padding=padding,
        border_radius=border_radius,
        min_width=min_width,
        min_height=min_height
    ))
    return button


def get_led_button_stylesheet(led_color: Literal["red", "blue"]) -> str:
    """
    Get stylesheet for circular LED button.
    
    Args:
        led_color: "red" or "blue"
    
    Returns:
        Stylesheet string
    """
    if led_color == "red":
        off_bg = Colors.LED_RED_OFF
        on_bg = Colors.LED_RED_ON
        off_border = Colors.LED_RED_BORDER_OFF
        on_border = Colors.LED_RED_BORDER_ON
        hover = Colors.LED_RED_HOVER
    else:  # blue
        off_bg = Colors.LED_BLUE_OFF
        on_bg = Colors.LED_BLUE_ON
        off_border = Colors.LED_BLUE_BORDER_OFF
        on_border = Colors.LED_BLUE_BORDER_ON
        hover = Colors.LED_BLUE_HOVER
    
    return f"""
        QPushButton {{
            background-color: {off_bg};
            color: white;
            border-radius: 28px;
            font-size: 9pt;
            font-weight: bold;
            border: 2px solid {off_border};
        }}
        QPushButton:hover {{
            background-color: {hover};
        }}
        QPushButton:checked {{
            background-color: {on_bg};
            border: 2px solid {on_border};
        }}
        QPushButton:disabled {{
            background-color: {Colors.BG_DARK};
            color: {Colors.TEXT_GRAY};
        }}
    """


def create_led_button(
    text: str,
    led_color: Literal["red", "blue"],
    size: int = 45,
    **kwargs
) -> QPushButton:
    """
    Create a circular LED button (red or blue).
    
    Args:
        text: Button text
        led_color: "red" or "blue"
        size: Fixed size for the button (default 45)
        **kwargs: Additional arguments to pass to QPushButton constructor
    
    Returns:
        Styled QPushButton instance
    """
    button = QPushButton(text, **kwargs)
    button.setCheckable(True)
    button.setFixedSize(size, size)
    button.setStyleSheet(get_led_button_stylesheet(led_color))
    return button


def create_increment_button(text: str = "▲", **kwargs) -> QPushButton:
    """
    Create an increment/decrement button with arrow styling.
    Blue fill with white arrow for enhanced contrast.
    
    Args:
        text: Button text (typically "▲" or "▼")
        **kwargs: Additional arguments to pass to QPushButton constructor
    
    Returns:
        Styled QPushButton instance
    """
    button = QPushButton(text, **kwargs)
    button.setStyleSheet(f"""
        QPushButton {{
            background-color: {Colors.PRIMARY_BLUE};
            color: white;
            font-size: 16pt;
            font-weight: bold;
            padding: 0px;
            border: 1px solid {Colors.PRIMARY_BLUE_HOVER};
            border-radius: 3px;
        }}
        QPushButton:hover {{
            background-color: {Colors.PRIMARY_BLUE_HOVER};
        }}
        QPushButton:pressed {{
            background-color: {Colors.PRIMARY_BLUE_PRESSED};
        }}
        QPushButton:disabled {{
            background-color: {Colors.BG_DISABLED};
            color: {Colors.TEXT_LIGHT_GRAY};
            border: 1px solid #a0a0a0;
        }}
    """)
    return button


# Status Label Functions

def get_status_label_stylesheet(
    status_type: Literal["success", "error", "warning", "info"],
    font_size: str = "12pt",
    font_weight: str = "bold"
) -> str:
    """
    Get stylesheet for status label.
    
    Args:
        status_type: Status type (success, error, warning, info)
        font_size: Font size string
        font_weight: Font weight string
    
    Returns:
        Stylesheet string
    """
    colors = {
        "success": Colors.STATUS_SUCCESS,
        "error": Colors.STATUS_ERROR,
        "warning": Colors.STATUS_WARNING,
        "info": Colors.STATUS_INFO
    }
    color = colors.get(status_type, Colors.TEXT_DARK)
    
    return f"color: {color}; font-size: {font_size}; font-weight: {font_weight};"


def create_status_label(
    text: str,
    status_type: Literal["success", "error", "warning", "info"] = "info",
    font_size: str = "12pt",
    font_weight: str = "bold",
    **kwargs
) -> QLabel:
    """
    Create a status label with color coding.
    
    Args:
        text: Label text
        status_type: Status type (success, error, warning, info)
        font_size: Font size string
        font_weight: Font weight string
        **kwargs: Additional arguments to pass to QLabel constructor
    
    Returns:
        Styled QLabel instance
    """
    label = QLabel(text, **kwargs)
    label.setStyleSheet(get_status_label_stylesheet(status_type, font_size, font_weight))
    return label


def get_status_badge_stylesheet(
    status_type: Literal["success", "error", "warning", "info", "gray"],
    padding: str = "4px",
    border_radius: str = "4px"
) -> str:
    """
    Get stylesheet for status badge.
    
    Args:
        status_type: Status type
        padding: Padding string
        border_radius: Border radius string
    
    Returns:
        Stylesheet string
    """
    colors = {
        "success": "green",
        "error": "red",
        "warning": "orange",
        "info": Colors.BLUE,
        "gray": Colors.CONNECTION_GRAY
    }
    bg_color = colors.get(status_type, Colors.CONNECTION_GRAY)
    text_color = "white" if status_type != "gray" else Colors.TEXT_DARK
    
    return f"background-color: {bg_color}; color: {text_color}; padding: {padding}; border-radius: {border_radius};"


def create_status_badge(
    text: str,
    status_type: Literal["success", "error", "warning", "info", "gray"] = "gray",
    padding: str = "4px",
    border_radius: str = "4px",
    alignment: Optional[Qt.AlignmentFlag] = None,
    **kwargs
) -> QLabel:
    """
    Create a badge-style status indicator.
    
    Args:
        text: Badge text
        status_type: Status type
        padding: Padding string
        border_radius: Border radius string
        alignment: Optional alignment flag
        **kwargs: Additional arguments to pass to QLabel constructor
    
    Returns:
        Styled QLabel instance
    """
    label = QLabel(text, **kwargs)
    label.setStyleSheet(get_status_badge_stylesheet(status_type, padding, border_radius))
    if alignment:
        label.setAlignment(alignment)
    return label


# Indicator Functions

def create_connection_indicator(label_text: str) -> QWidget:
    """
    Create a connection status indicator widget with circular indicator and text.
    
    Args:
        label_text: Text label for the indicator
    
    Returns:
        QWidget containing the indicator with attributes:
        - indicator_circle: QLabel (the circular indicator)
        - text_label: QLabel (the text label)
    """
    indicator_widget = QWidget()
    indicator_layout = QHBoxLayout(indicator_widget)
    indicator_layout.setContentsMargins(5, 5, 5, 5)
    indicator_layout.setSpacing(10)
    
    # Circular indicator (using QLabel with styled background)
    indicator_circle = QLabel()
    indicator_circle.setFixedSize(20, 20)
    indicator_circle.setStyleSheet(f"""
        QLabel {{
            background-color: {Colors.CONNECTION_GRAY};
            border-radius: 10px;
            border: 2px solid {Colors.CONNECTION_GRAY_BORDER};
        }}
    """)
    indicator_layout.addWidget(indicator_circle)
    
    # Text label
    text_label = QLabel(label_text)
    text_label.setStyleSheet("font-size: 11pt;")
    indicator_layout.addWidget(text_label)
    
    indicator_layout.addStretch()
    
    # Store references for easy access
    indicator_widget.indicator_circle = indicator_circle
    indicator_widget.text_label = text_label
    
    return indicator_widget


def update_connection_indicator(
    indicator_widget: QWidget,
    status: Literal["connected", "disconnected", "failed"]
) -> None:
    """
    Update connection indicator appearance based on status.
    
    Args:
        indicator_widget: The indicator widget to update (created by create_connection_indicator)
        status: "disconnected" (gray), "connected" (green), or "failed" (red)
    """
    if status == "connected":
        color = Colors.CONNECTION_GREEN
        border_color = Colors.CONNECTION_GREEN_BORDER
    elif status == "failed":
        color = Colors.CONNECTION_RED
        border_color = Colors.CONNECTION_RED_BORDER
    else:  # disconnected
        color = Colors.CONNECTION_GRAY
        border_color = Colors.CONNECTION_GRAY_BORDER
    
    indicator_widget.indicator_circle.setStyleSheet(f"""
        QLabel {{
            background-color: {color};
            border-radius: 10px;
            border: 2px solid {border_color};
        }}
    """)


def create_text_indicator(label_text: str) -> QWidget:
    """
    Create a text indicator widget with label and value using badge style.
    
    Args:
        label_text: Label text (will have ":" appended)
    
    Returns:
        QWidget containing the indicator with attribute:
        - value_label: QLabel (the value display)
    """
    indicator_widget = QFrame()
    # Remove default frame styling to prevent any borders
    indicator_widget.setFrameShape(QFrame.Shape.NoFrame)
    indicator_widget.setFrameShadow(QFrame.Shadow.Plain)
    # Apply badge style background with rounded corners to the container
    # Explicitly set no border to ensure only one box appears
    indicator_widget.setStyleSheet(f"""
        QFrame {{
            background-color: {Colors.BG_BADGE};
            border-radius: 8px;
            border: none;
        }}
    """)
    
    indicator_layout = QHBoxLayout(indicator_widget)
    indicator_layout.setContentsMargins(12, 5, 12, 5)
    indicator_layout.setSpacing(8)
    
    # Label with badge style colors - ensure completely transparent with no borders
    label = QLabel(f"{label_text}:")
    label.setAutoFillBackground(False)
    label.setStyleSheet(f"""
        QLabel {{
            font-size: 11pt;
            font-weight: bold;
            color: {Colors.TEXT_BADGE_LABEL};
            background-color: transparent;
            border: none;
            padding: 0px;
            margin: 0px;
        }}
    """)
    indicator_layout.addWidget(label)
    
    # Value label with badge style colors - ensure completely transparent with no borders
    value_label = QLabel("N/A")
    value_label.setAutoFillBackground(False)
    value_label.setStyleSheet(f"""
        QLabel {{
            font-size: 11pt;
            color: {Colors.TEXT_BADGE_VALUE};
            background-color: transparent;
            border: none;
            padding: 0px;
            margin: 0px;
        }}
    """)
    indicator_layout.addWidget(value_label)
    
    indicator_layout.addStretch()
    
    # Store reference for easy access
    indicator_widget.value_label = value_label
    
    return indicator_widget


# Input Field Functions

def get_input_stylesheet(
    border_color: str = Colors.BORDER_LIGHT,
    border_radius: str = "3px",
    padding: str = "5px",
    background_color: str = Colors.BG_WHITE,
    font_size: str = "11pt"
) -> str:
    """
    Get stylesheet for input fields (QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox).
    
    Args:
        border_color: Border color
        border_radius: Border radius string
        padding: Padding string
        background_color: Background color
        font_size: Font size string
    
    Returns:
        Stylesheet string
    """
    return f"border: 1px solid {border_color}; border-radius: {border_radius}; padding: {padding}; background-color: {background_color}; font-size: {font_size};"


def style_input_field(
    widget,
    border_color: str = Colors.BORDER_LIGHT,
    border_radius: str = "3px",
    padding: str = "5px",
    background_color: str = Colors.BG_WHITE,
    font_size: str = "11pt"
) -> None:
    """
    Apply consistent styling to input fields.
    
    Args:
        widget: QLineEdit, QSpinBox, QDoubleSpinBox, or QComboBox instance
        border_color: Border color
        border_radius: Border radius string
        padding: Padding string
        background_color: Background color
        font_size: Font size string
    """
    widget.setStyleSheet(get_input_stylesheet(
        border_color=border_color,
        border_radius=border_radius,
        padding=padding,
        background_color=background_color,
        font_size=font_size
    ))


# Progress Bar Functions

def get_progress_bar_stylesheet(
    chunk_color: Literal["blue", "green"] = "blue",
    border_color: str = Colors.BORDER_MEDIUM,
    border_radius: str = "5px",
    font_size: str = "10pt",
    height: str = "25px"
) -> str:
    """
    Get stylesheet for progress bar.
    
    Args:
        chunk_color: Color of the progress chunk ("blue" or "green")
        border_color: Border color
        border_radius: Border radius string
        font_size: Font size string
        height: Height string
    
    Returns:
        Stylesheet string
    """
    chunk_colors = {
        "blue": Colors.BLUE,
        "green": Colors.SUCCESS_GREEN
    }
    chunk_color_value = chunk_colors.get(chunk_color, Colors.BLUE)
    
    return f"""
        QProgressBar {{
            border: 2px solid {border_color};
            border-radius: {border_radius};
            text-align: center;
            font-size: {font_size};
            height: {height};
        }}
        QProgressBar::chunk {{
            background-color: {chunk_color_value};
            border-radius: 3px;
        }}
    """


def create_progress_bar(
    chunk_color: Literal["blue", "green"] = "blue",
    border_color: str = Colors.BORDER_MEDIUM,
    border_radius: str = "5px",
    font_size: str = "10pt",
    height: str = "25px",
    **kwargs
):
    """
    Create a styled progress bar.
    
    Args:
        chunk_color: Color of the progress chunk ("blue" or "green")
        border_color: Border color
        border_radius: Border radius string
        font_size: Font size string
        height: Height string
        **kwargs: Additional arguments to pass to QProgressBar constructor
    
    Returns:
        Styled QProgressBar instance
    """
    from PySide6.QtWidgets import QProgressBar
    progress_bar = QProgressBar(**kwargs)
    progress_bar.setStyleSheet(get_progress_bar_stylesheet(
        chunk_color=chunk_color,
        border_color=border_color,
        border_radius=border_radius,
        font_size=font_size,
        height=height
    ))
    return progress_bar


# Checkbox Functions

def get_checkbox_stylesheet(
    indicator_size: str = "18px",
    border_color: str = Colors.BORDER_MEDIUM,
    checked_color: str = Colors.PRIMARY_BLUE,
    checked_border_color: str = Colors.PRIMARY_BLUE_HOVER
) -> str:
    """
    Get stylesheet for checkbox.
    
    Args:
        indicator_size: Size of the checkbox indicator
        border_color: Border color when unchecked
        checked_color: Background color when checked
        checked_border_color: Border color when checked
    
    Returns:
        Stylesheet string
    """
    return f"""
        QCheckBox::indicator {{
            width: {indicator_size};
            height: {indicator_size};
            border: 2px solid {border_color};
            border-radius: 3px;
            background-color: white;
        }}
        QCheckBox::indicator:checked {{
            background-color: {checked_color};
            border: 2px solid {checked_border_color};
        }}
        QCheckBox::indicator:checked:hover {{
            background-color: {checked_border_color};
            border: 2px solid {checked_border_color};
        }}
    """


def style_checkbox(
    checkbox,
    indicator_size: str = "18px",
    border_color: str = Colors.BORDER_MEDIUM,
    checked_color: str = Colors.PRIMARY_BLUE,
    checked_border_color: str = Colors.PRIMARY_BLUE_HOVER
) -> None:
    """
    Apply consistent styling to checkbox.
    
    Args:
        checkbox: QCheckBox instance
        indicator_size: Size of the checkbox indicator
        border_color: Border color when unchecked
        checked_color: Background color when checked
        checked_border_color: Border color when checked
    """
    checkbox.setStyleSheet(get_checkbox_stylesheet(
        indicator_size=indicator_size,
        border_color=border_color,
        checked_color=checked_color,
        checked_border_color=checked_border_color
    ))
