"""Chinese labels for native message dialogs, independent of the OS language."""
from __future__ import annotations

import wx


def message_dialog(
    parent: wx.Window | None,
    message: str,
    caption: str = "提示",
    style: int = wx.OK | wx.CENTRE,
    pos: wx.Point = wx.DefaultPosition,
) -> wx.MessageDialog:
    dialog = wx.MessageDialog(parent, message, caption, style, pos)
    # Set native labels before ShowModal: message box buttons are not wx child
    # windows and cannot reliably be renamed through FindWindow.
    if style & wx.YES_NO:
        if style & wx.CANCEL:
            dialog.SetYesNoCancelLabels("是(&Y)", "否(&N)", "取消")
        else:
            dialog.SetYesNoLabels("是(&Y)", "否(&N)")
    elif style & wx.CANCEL:
        dialog.SetOKCancelLabels("确定", "取消")
    else:
        dialog.SetOKLabel("确定")
    if style & wx.HELP:
        dialog.SetHelpLabel("帮助")
    return dialog


def message_box(
    message: str,
    caption: str = "提示",
    style: int = wx.OK | wx.CENTRE,
    parent: wx.Window | None = None,
    x: int = wx.DefaultCoord,
    y: int = wx.DefaultCoord,
) -> int:
    """Like wx.MessageBox, including its flag-valued (not ID-valued) result."""
    dialog = message_dialog(parent, message, caption, style, wx.Point(x, y))
    try:
        result = dialog.ShowModal()
        return {
            wx.ID_OK: wx.OK,
            wx.ID_CANCEL: wx.CANCEL,
            wx.ID_YES: wx.YES,
            wx.ID_NO: wx.NO,
            wx.ID_HELP: wx.HELP,
        }.get(result, wx.CANCEL)
    finally:
        dialog.Destroy()
