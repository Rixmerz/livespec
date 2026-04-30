// TypeScript fixture: typed declarations, arrow functions, class with method

function helper(x: number): number {
  return x * 2;
}

function topLevelOne(x: number): number {
  return helper(x);
}

const arrowFn = (x: number): number => helper(x + 1);

const arrowFnBlock = (x: number): number => {
  return helper(x);
};

class Greeter {
  name: string;
  constructor(name: string) {
    this.name = name;
  }

  greet(): number {
    return helper(this.name.length);
  }
}
