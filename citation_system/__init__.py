"""Independent catalog + citation repository.

The package deliberately does not read PDFs or images.  A volume is an ordered
list of physical pages; a page identity is ``(volume_id, page_id)``.  Citation
products are exported as JSONL and can be consumed without opening SQLite.
"""

__version__ = "1.0.0"
