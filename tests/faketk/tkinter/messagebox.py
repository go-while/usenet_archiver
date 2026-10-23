SHOWN = []  # (kind, title, message)
ANSWER = [True]  # what askokcancel answers


def showerror(title, message):
    SHOWN.append(("error", title, message))


def showwarning(title, message):
    SHOWN.append(("warning", title, message))


def showinfo(title, message):
    SHOWN.append(("info", title, message))


def askokcancel(title, message):
    SHOWN.append(("ask", title, message))
    return ANSWER[0]
