# Evernote para HTML e Markdown · v1.5

[English](README.en.md)

Conversor gratuito e local de exportações Evernote (.enex). Gera uma pasta por caderno e um ficheiro por nota, com imagens, anexos e tarefas. Funciona por interface gráfica ou linha de comandos e inclui recuperação parcial de XML danificado.

## Início rápido

Descarrega `enexconverter-pt.py`. Precisas de Python 3.8 ou superior, sem pacotes para instalar com pip. A interface gráfica também requer Tkinter e Tcl/Tk. Podes verificar a instalação com `python -m tkinter`. Se o teu sistema usar o comando `python3`, substitui `python` nos exemplos.

```sh
# Abrir a janela
python enexconverter-pt.py

# Converter dois cadernos em HTML
python enexconverter-pt.py "Trabalho.enex" "Notas pessoais.enex"

# Converter os cadernos da pasta atual em Markdown
python enexconverter-pt.py --markdown .

# Consultar a ajuda
python enexconverter-pt.py --help
```

Na janela, seleciona um ou mais ficheiros ou uma pasta, escolhe **Markdown (.md)** se preferires esse formato e clica em **Iniciar Conversão**. O destino em branco guarda cada caderno junto ao original. A conversão corre em segundo plano, com barra de progresso, contadores e registo de atividade. Na CLI podes combinar caminhos de ficheiros e pastas. A pesquisa em pastas é não recursiva.

## O que é exportado

| Conteúdo | Resultado |
|---|---|
| HTML (predefinido) | Página UTF-8 com título, tabelas, imagens responsivas e ligações relativas para os ficheiros em `_anexos/`. Abre a página num navegador. |
| Markdown | Texto UTF-8 com títulos, listas, tabelas, negrito, itálico, citações, código e ligações. As imagens do ENEX ficam embutidas em Base64 com o respetivo tipo MIME. PDFs e outros anexos permanecem em `_anexos/`. |
| Tarefas | Reconhece caixas de seleção e registos de tarefas compatíveis. Representa o estado pendente ou concluído e remove o aviso conhecido do Evernote sobre o espaço reservado para tarefas. Se o ENEX contiver apenas o aviso, a descrição da tarefa não pode ser recuperada. |
| Recursos | Descodifica os anexos Base64, associa-os ao corpo da nota pelo MD5 usado pelo Evernote e usa o nome original ou uma extensão inferida do MIME. Assinala referências sem ficheiro correspondente. |
| Nomes e cadernos grandes | Higieniza os nomes para Windows, abrevia nomes compridos e acrescenta sufixos numéricos para evitar colisões. Lê o XML de forma incremental e liberta cada nota e recurso após o processamento. O maior recurso individual influencia o consumo de memória. |

Por exemplo, `Trabalho.enex` gera `Trabalho/Reunião.md` e pode guardar um PDF em `Trabalho/_anexos/orçamento.pdf`. Para sincronizar com Google Drive ou outro serviço, copia a pasta inteira. A apresentação de imagens embutidas e a abertura de ligações relativas dependem do visualizador. A v1.5 não decifra texto protegido pelo Evernote. Conserva o ENEX original.

## Recuperação e leitura do registo

- O conversor escreve em `<caderno>.enex-partial` e muda o nome da pasta ao finalizar. Protege destinos existentes. Se uma tentativa for interrompida, escolhe um novo destino ou move a pasta parcial antes de repetir.
- Se o corpo ENML for ilegível, tenta preservar o conteúdo como texto. Se encontrar XML inválido no caderno, tenta corrigir um `&` isolado ou remover um carácter de controlo proibido. Só aceita a reparação após validar a nota. O ficheiro de origem fica intacto.
- Se não conseguir reparar a nota, tenta guardar o fragmento original em `_recovery/` e retomar na próxima nota identificável. O aviso indica a posição do erro e, quando disponíveis, o título, as etiquetas incompatíveis e o caminho do fragmento.
- **“Cadernos concluídos: 1/1”** indica que a pasta foi finalizada. Verifica também os avisos, as notas reparadas e os fragmentos por recuperar. Uma nota convertida pode conter uma referência a um anexo em falta.
- Na CLI, o código de saída é `0` para sucesso sem avisos, `1` para avisos ou falhas de conversão e `2` para problemas de entrada ou de arranque da interface.
