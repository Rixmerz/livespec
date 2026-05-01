<?php

use Service\Greeter;

function run(): string {
    return Greeter::makeDefault();
}
