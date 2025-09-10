# Cross-module shared state

from typing import Optional, Dict

# אחרון DTM/אורתופוטו שנטענו (לשימוש בין הטאבים)
dtm_path: Optional[str] = None
orthophoto_path: Optional[str] = None

# מיקום המצלמה במערכת הקואורדינטות של הרסטר ממנו נלקח (פרויקטד)
# נשמר כדי לצייר CAM גם בטאב Image→Ground
# מבנה: {"x": float, "y": float, "epsg": int}
camera_proj: Optional[Dict] = None
