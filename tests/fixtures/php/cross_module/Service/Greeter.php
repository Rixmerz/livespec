<?php

namespace Service;

class Greeter {
    public function greet(): string {
        return "hello";
    }

    public static function makeDefault(): string {
        return "default";
    }
}
