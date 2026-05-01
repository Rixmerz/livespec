<?php

function helper($x) {
    return $x * 2;
}

function topLevelOne($x) {
    return helper($x);
}

class Greeter {
    private $name;

    public function __construct($name) {
        $this->name = $name;
    }

    public function greet() {
        return helper(strlen($this->name));
    }
}
