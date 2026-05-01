# Ruby fixture: top-level methods, class with instance method, cross call.

def helper(x)
  x * 2
end

def top_level_one(x)
  helper(x)
end

class Greeter
  def initialize(name)
    @name = name
  end

  def greet
    helper(@name.length)
  end
end
