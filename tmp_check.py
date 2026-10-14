from app.parsers.report_loader import ReportHeaderParser
from pathlib import Path
p = Path('REPORT/“ест“овары¬ѕути.txt')
df, h = ReportHeaderParser.load_inventory_csv(p)
print(df.shape)
print(list(df.columns))
