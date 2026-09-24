from __future__ import annotations

from .runtime import *

class ClosableNotebook(ttk.Notebook):
    """
    ttk.Notebook with a real close icon rendered inside every tab.
    The widget does not close tabs itself: it emits <<NotebookCloseRequested>>
    so the application can reject closing a running conversion safely.
    """

    def __init__(self, master=None, **kwargs) -> None:
        self._build_close_style(master)
        kwargs["style"] = "ClosableNotebook"
        super().__init__(master, **kwargs)
        self._close_pressed_index: int | None = None
        self.close_requested_index: int | None = None

        self.bind("<ButtonPress-1>", self._on_close_press, add="+")
        self.bind("<ButtonRelease-1>", self._on_close_release, add="+")

    def _build_close_style(self, master) -> None:
        style = ttk.Style(master)

        # Keep images on the instance later; Tcl keeps named images, but Python
        # references are also retained by assigning them to the root.
        root = master.winfo_toplevel() if master is not None else None

        normal = tk.PhotoImage(width=9, height=9, master=root)
        active = tk.PhotoImage(width=9, height=9, master=root)
        pressed = tk.PhotoImage(width=9, height=9, master=root)

        def draw_cross(image: tk.PhotoImage, color: str) -> None:
            for offset in (0, 1):
                for i in range(1, 8):
                    x1 = min(8, i + offset)
                    x2 = max(0, 8 - i - offset)
                    image.put(color, to=(x1, i))
                    image.put(color, to=(x2, i))

        draw_cross(normal, "#666666")
        draw_cross(active, "#cc3333")
        draw_cross(pressed, "#992222")

        if root is not None:
            root._closable_notebook_images = (normal, active, pressed)  # type: ignore[attr-defined]

        try:
            style.element_create(
                "ClosableNotebook.close",
                "image",
                normal,
                ("active", "!disabled", active),
                ("pressed", "!disabled", pressed),
                border=2,
                sticky="",
            )
        except tk.TclError:
            # The element may already exist if another notebook was created.
            pass

        style.layout(
            "ClosableNotebook",
            [("Notebook.client", {"sticky": "nswe"})],
        )
        style.layout(
            "ClosableNotebook.Tab",
            [
                (
                    "Notebook.tab",
                    {
                        "sticky": "nswe",
                        "children": [
                            (
                                "Notebook.padding",
                                {
                                    "side": "top",
                                    "sticky": "nswe",
                                    "children": [
                                        (
                                            "Notebook.focus",
                                            {
                                                "side": "top",
                                                "sticky": "nswe",
                                                "children": [
                                                    (
                                                        "Notebook.label",
                                                        {
                                                            "side": "left",
                                                            "sticky": "",
                                                        },
                                                    ),
                                                    (
                                                        "ClosableNotebook.close",
                                                        {
                                                            "side": "left",
                                                            "sticky": "",
                                                        },
                                                    ),
                                                ],
                                            },
                                        )
                                    ],
                                },
                            )
                        ],
                    },
                )
            ],
        )

    def _on_close_press(self, event):
        element = self.identify(event.x, event.y)
        if "close" not in element:
            return None

        try:
            index = self.index(f"@{event.x},{event.y}")
        except tk.TclError:
            return None

        self._close_pressed_index = index
        self.state(["pressed"])
        return "break"

    def _on_close_release(self, event):
        if self._close_pressed_index is None:
            return None

        pressed_index = self._close_pressed_index
        self._close_pressed_index = None
        self.state(["!pressed"])

        element = self.identify(event.x, event.y)
        if "close" not in element:
            return "break"

        try:
            index = self.index(f"@{event.x},{event.y}")
        except tk.TclError:
            return "break"

        if index == pressed_index:
            self.close_requested_index = index
            self.event_generate("<<NotebookCloseRequested>>")

        return "break"
