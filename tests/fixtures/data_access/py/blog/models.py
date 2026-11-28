from django.db import models


class Author(models.Model):
    name = models.CharField(max_length=10)


class Book(models.Model):
    title = models.CharField(max_length=10)

    class Meta:
        db_table = "library_books"


def authors():
    return list(Author.objects.filter(name="x"))


def make_author():
    return Author.objects.create(name="y")


def drop_books():
    Book.objects.all().delete()
