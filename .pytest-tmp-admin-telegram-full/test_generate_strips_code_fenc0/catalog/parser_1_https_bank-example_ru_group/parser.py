import scrapy, json

class LoopholeSpider(scrapy.Spider):
    name = "loophole"
    def parse(self, response):
        yield {"title": "test", "url": response.url}
