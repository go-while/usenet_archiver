NEXT = [""]  # the path the next dialog returns


def askopenfilename(**kw):
    return NEXT[0]


def askdirectory(**kw):
    return NEXT[0]
