# Standard Libra
import os
import math
import sqlite3
import operator
import requests
from typing import Annotated, Sequence, TypedDict, Literal

# Third-party utilities
from pydantic import BaseModel, Field
from dotenv import load_dotenv

# LangChain Core & Community
from langchain_core.messages import BaseMessage, SystemMessage, HumanMessage, ToolMessage, AIMessage
from langchain_core.tools import tool
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_community.document_loaders import PyPDFLoader
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_groq import ChatGroq
from langchain_tavily import TavilySearch
from langchain_community.vectorstores import FAISS

# LangGraph
from langgraph.graph import START, END, StateGraph
from langgraph.graph.message import add_messages
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.checkpoint.memory import MemorySaver
from langgraph.prebuilt import ToolNode, tools_condition
from langgraph.types import interrupt, Command


load_dotenv()

llm = ChatGroq(model="qwen/qwen3.6-27b", reasoning_effort="none", temperature=0)
embeddings = HuggingFaceEmbeddings(
    model_name="sentence-transformers/all-MiniLM-L6-v2",
    model_kwargs={"local_files_only": False}
)

def ingest_rag_document(file_path):
    DB_PATH = "faiss_db"
    loader = PyPDFLoader(file_path)
    docs = loader.load()
    splitter = RecursiveCharacterTextSplitter(chunk_size = 1000, chunk_overlap = 200)
    chunks = splitter.split_documents(docs)
    vectorstore = FAISS.from_documents(chunks, embeddings)
    vectorstore.save_local(DB_PATH)

def get_retriever():
    DB_PATH = "faiss_db"
    vectorstore = FAISS.load_local(folder_path=DB_PATH, embeddings=embeddings, allow_dangerous_deserialization=True)
    
    retriever = vectorstore.as_retriever(search_type="similarity", search_kwargs={"k": 5})
    return retriever

@tool
def retriever_tool(query:str)->str:
    """This tool searches and return information from the RAG Guide PDF"""

    retriever = get_retriever()
    docs = retriever.invoke(query)

    if not docs:
        return "No relevant information found related to the RAG Guide."
    
    results = []
    for i, doc in enumerate(docs):
        results.append(f"Document {i+1}:\n{doc.page_content}\n")

    return "\n\n".join(results)

# Tools
search_tool = TavilySearch(max_results=5, topic="general", search_depth="advanced")
@tool
def calculator(expression:str)-> str:
    """
    useful for simple math calcaulations.
    Input should be valid math expression.
    Example: 2+2, math.sqrt(16), 10 * 5
    """
    try:
        allowed = {
            "math" : math,
            "abs" : abs,
            "round" : round,
            "sum" : sum,
            "max" : max,
            "min" : min,
        }

        result = eval(expression, {"__builtins__" : None}, allowed)
        return str(result)
    except Exception as e:
        return f"Error: {str(e)}"

@tool
def get_stock_price(symbol: str) -> dict:
    """
    Get stock price for a given symbol. (e.g 'MSFT', 'AAPL', 'GOOGL')
    using Alpha Vantage with API key in the URL.
    """
    url = f"https://www.alphavantage.co/query?function=GLOBAL_QUOTE&symbol={symbol}&apikey=REDACTED"
    response = requests.get(url)
    return response.json()


# Dummy Tool
@tool
def purchase_stock(symbol: str, quantity: int) -> dict:
    """
    Simulate purchasing a given quantity of a stock symbol.
    
    HUMAN-IN-THE-LOOP:
    before confirming the purchase, this tool will interrupt
    and wait for a human decision ("yes" /anything else).
    """
    decision =  interrupt(f"Approve buying {quantity} shares of {symbol}?yes/no")

    if isinstance(decision,str) and decision.lower() == "yes":
        return {"message": f"Approved! You bought {quantity} shares of {symbol}."}
    else:
        return {"message": "Not approved"}





@tool
def get_weather(city: str, units: str = "metric") -> str:
    """
    Fetch current weather conditions for a given city using the OpenWeather API.
 
    Args:
        city: Name of the city (e.g. "Lahore", "Karachi,PK", "London,GB").
              Adding a country code after a comma improves match accuracy.
        units: Unit system - "metric" (Celsius), "imperial" (Fahrenheit),
               or "standard" (Kelvin). Defaults to "metric".
 
    Returns:
        A human-readable summary of current weather conditions, or an
        error message if the lookup fails.
    """
    api_key = os.environ.get("OPENWEATHER_API_KEY")
    if not api_key:
        return "Error: OPENWEATHER_API_KEY environment variable is not set."
 
    url = "https://api.openweathermap.org/data/2.5/weather"
    params = {"q": city, "appid": api_key, "units": units}
 
    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
    except requests.exceptions.HTTPError:
        status = response.status_code
        if status == 404:
            return f"Error: City '{city}' not found."
        elif status == 401:
            return "Error: Invalid OpenWeather API key."
        else:
            return f"Error: OpenWeather API returned status {status}."
    except requests.exceptions.RequestException as e:
        return f"Error: Failed to reach OpenWeather API - {e}"
 
    data = response.json()
    unit_symbol = {"metric": "°C", "imperial": "°F", "standard": "K"}.get(units, "")
 
    description = data["weather"][0]["description"].capitalize()
    temp = data["main"]["temp"]
    feels_like = data["main"]["feels_like"]
    humidity = data["main"]["humidity"]
    wind_speed = data["wind"]["speed"]
    location_name = data.get("name", city)
    country = data.get("sys", {}).get("country", "")
 
    return (
        f"Weather in {location_name}, {country}: {description}. "
        f"Temperature: {temp}{unit_symbol} (feels like {feels_like}{unit_symbol}). "
        f"Humidity: {humidity}%. Wind speed: {wind_speed} m/s."
    )

tools = [retriever_tool, search_tool, calculator, get_stock_price, get_weather,purchase_stock]

llm_with_tools = llm.bind_tools(tools)

DEFAULT_MODEL = "qwen/qwen3.6-27b"
current_model = DEFAULT_MODEL


def set_model(model_id: str, provider: str = "groq"):
    """Swap the chat model used by chat_node and the tool binding.

    chat_node looks up llm_with_tools as a module global at call time, so
    reassigning it here changes the model for every following turn without
    rebuilding the graph.

    Note: reasoning_effort="none" is only accepted by the qwen models; the
    gpt-oss models reject anything other than low/medium/high.
    """
    global llm, llm_with_tools, current_model

    if model_id == current_model:
        return llm

    if provider == "google":
        from langchain_google_genai import ChatGoogleGenerativeAI
        llm = ChatGoogleGenerativeAI(model=model_id, temperature=0)
    elif model_id.startswith("qwen"):
        llm = ChatGroq(model=model_id, reasoning_effort="none", temperature=0)
    else:
        llm = ChatGroq(model=model_id, temperature=0)

    llm_with_tools = llm.bind_tools(tools)
    current_model = model_id
    return llm


# Built from the actual tool objects. A hand-written list drifts out of date,
# and a prompt naming tools that do not exist makes the model distrust real
# tool results and ask the user to restate the question instead.
TOOL_GUIDE = "\n".join(
    "- `%s`: %s" % (t.name, (t.description or "").strip().splitlines()[0])
    for t in tools
)


# State
class Chatstate(TypedDict):
    messages : Annotated[list[BaseMessage],add_messages] # reducer is applied automatically to the messages list

# Chat Node
def chat_node(state:Chatstate):
    """LLM node that may answer or request tool call"""

    System_Message = SystemMessage(
        content = (
            "You are a helpful Agentic Chatbot with access to these tools:\n\n"
            + TOOL_GUIDE +
            "\n\nAnswer general questions directly when no tool is required. "
            "Do not invent information from the uploaded document. "
            "If the user asks about a PDF but no document is available, ask them to upload a PDF.\n\n"

            "After a tool returns, ANSWER FROM THAT RESULT. Never reply by asking the "
            "user to repeat or clarify something they already asked - the tool output is "
            "the answer to their question. If a tool returned several entries, use the one "
            "matching the request and ignore the rest. A bare name or topic from the user "
            "means: tell me about this."
        )
    )
    # take user query from state
    messages = [System_Message, *state["messages"]]
    # invoke llm
    response = llm_with_tools.invoke(messages)
    # response added to the messages list
    return {"messages": [response]}

# Tool Node
tool_node = ToolNode(tools)

connection = sqlite3.connect(database = "agentic_chatbot.db", check_same_thread=False)
checkpoint = SqliteSaver(connection)

graph = StateGraph(Chatstate)

# Nodes
graph.add_node("chat_node", chat_node)
graph.add_node("tools", tool_node)

# Edges
graph.add_edge(START, "chat_node")
graph.add_conditional_edges(
    "chat_node",
    tools_condition,
)
graph.add_edge("tools", "chat_node")

chatbot =graph.compile(checkpointer=checkpoint)

# Helper functions for FastAPI endpoints
def get_all_threads():
    all_threads = set()
    for ckpt in checkpoint.list(None):
        all_threads.add(ckpt.config.get("configurable", {}).get("thread_id"))

    return list(all_threads)

if __name__ == "__main__":
    print("🤖 Agentic Chatbot CLI\n")
    print("Type 'exit' to quit.\n")

    # thread_id still works with MemorySaver (conversation kept in RAM)
    thread_id = "demo-thread"

    while True:
        user_input = input("You: ")
        if user_input.lower().strip() in {"exit", "quit"}:
            print("Goodbye!")
            break

        config = {"configurable": {"thread_id": thread_id}}
        state_snapshot = chatbot.get_state(config)
        
        # Check if graph is currently interrupted
        if state_snapshot.next:
            # Resume it with the user's input
            result = chatbot.invoke(Command(resume=user_input), config=config)
        else:
            # Start a new turn
            state = {"messages": [HumanMessage(content=user_input)]}
            result = chatbot.invoke(state, config=config)

        # Check if graph paused again after invocation
        new_state = chatbot.get_state(config)
        if new_state.next and new_state.tasks and new_state.tasks[0].interrupts:
            # It was interrupted. Print the interrupt message.
            interrupt_val = new_state.tasks[0].interrupts[0].value
            print(f"System: {interrupt_val}\n")
        else:
            # Completed the run. Print the assistant's final response
            latest_message = result["messages"][-1]
            print(f"Assistant: {latest_message.content}\n")