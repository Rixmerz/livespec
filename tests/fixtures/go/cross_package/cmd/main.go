package main

import (
	"example.com/proj/util"
	alias "example.com/proj/util"
)

func Run() string {
	x := util.Helper()
	return alias.Format(x)
}
