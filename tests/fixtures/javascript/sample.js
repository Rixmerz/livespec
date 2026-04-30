// JavaScript fixture: function declarations, arrow functions, class

function helper(x) {
  return x * 2;
}

function topLevelOne(x) {
  return helper(x);
}

const arrowFn = (x) => helper(x + 1);

const arrowFnBlock = (x) => {
  return helper(x);
};

class Greeter {
  constructor(name) {
    this.name = name;
  }

  greet() {
    return helper(this.name.length);
  }
}
