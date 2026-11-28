import { Entity, Repository } from "typeorm";

@Entity("widgets")
export class Widget {
  id!: number;
}

export class WidgetService {
  constructor(private repo: Repository<Widget>) {}

  all() {
    return this.repo.find();
  }

  remove(w: Widget) {
    return this.repo.remove(w);
  }
}
